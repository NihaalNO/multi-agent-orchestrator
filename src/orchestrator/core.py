"""Orchestrator facade: the full 12-stage request lifecycle behind one interface.

    submit(text, principal)
      1  intake            validate the request; honour a client idempotency key
      2–5 plan + route     planner proposes, router binds to permitted tools  (RoutingService)
      6  authorize         plan-time permission check                         (PermissionManager)
      7–9 execute          DAG scheduling, final permission checks, approvals (DagScheduler)
      10 resolve           conflict detection and resolution                  (ConflictResolver)
      11–12 respond        consolidated record with summary and stage timings

    resume(request_id, principal)
      re-runs only what did not succeed (e.g. after approvals were decided), reusing earlier
      outputs, then resolves and summarises again.

Requests that cannot be planned, routed or authorized end ``rejected`` with a structured error;
nothing is executed for them. Records are held in a ``RequestStore``: in memory by default, or in
SQLite when ``ORCH_STATE_PATH`` is set (``orchestrator.state``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.answers import Answer, AnswerComposer, build_composer, compose_answer, earlier_evidence
from orchestrator.coding import CodeAnswer, CodeWriter, build_code_writer, latest_code, wants_code
from orchestrator.config import Settings
from orchestrator.receipts import Receipt, ReceiptChain, ReceiptIssuer, Signer
from orchestrator.contracts import (
    ApprovalStatus,
    ErrorInfo,
    ExecutionPlan,
    ExecutionReport,
    ExecutionStatus,
    HistoryTurn,
    IsolationLevel,
    PlanningRequest,
    Principal,
    RoutingDecision,
    TaskStatus,
    UnroutedTask,
)
from orchestrator.errors import (
    ApprovalRequiredError,
    InvalidRequestStateError,
    NoMatchingToolError,
    OrchestratorError,
    PermissionDeniedError,
    RequestNotFoundError,
)
from orchestrator.execution import DagScheduler, ExecutionEngine
from orchestrator.repair import AttemptSummary, build_feedback, repairable_failures, reusable_results, shape
from orchestrator.execution.coordination import IdempotencyBackend
from orchestrator.observability.audit import AuditLog
from orchestrator.permissions import ApprovalManager, PermissionManager, PlanAuthorization
from orchestrator.registry.registry import ToolRegistry
from orchestrator.resolution import ConflictResolver
from orchestrator.routing import Planner, RoutingService, build_planner, build_proposal
from orchestrator.tools import FileProcessingTool, SearchBackend, WebSearchTool
from orchestrator.tools.python_exec import build_python_tool

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator.replay import ReplayReport


class RequestState(StrEnum):
    RUNNING = "running"
    REJECTED = "rejected"            # could not be planned, routed or authorized; nothing ran
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"          # everything, or part of it, succeeded (see report.status)
    FAILED = "failed"                # execution ran but nothing succeeded


MAX_CONTEXT = 5  # earlier requests a follow-up can build on


def _approver(approvals: ApprovalManager):
    def who(approval_id: str) -> str | None:
        try:
            return approvals.get(approval_id).decided_by
        except Exception:  # an expired or unknown approval: the receipt records the id only
            return None
    return who


def _outputs(record: "RequestRecord") -> dict[str, Any]:
    """Outputs of the steps that succeeded in a finished request, by task id."""
    if record.report is None:
        return {}
    return {r.task_id: r.output for r in record.report.task_results
            if r.status == TaskStatus.SUCCEEDED and r.output is not None}


def _history(turns: list[tuple[str, "RequestRecord"]]) -> tuple[HistoryTurn, ...]:
    """What the planner learns about earlier turns: question, answer, and each step's output shape,
    plus the value when it is a single number or short string (the planner still references it)."""
    history = []
    for alias, earlier in turns:
        steps = {}
        for task_id, output in _outputs(earlier).items():
            summary = shape(output)
            value = output.get("result") if isinstance(output, dict) else None
            if isinstance(value, (int, float)) or (isinstance(value, str) and len(value) <= 120):
                summary += f"; result = {value!r}"
            steps[task_id] = summary
        answer = earlier.answer.text[:600] if earlier.answer else None
        history.append(HistoryTurn(alias=alias, text=earlier.text[:600], answer=answer, steps=steps))
    return tuple(history)


def _question(text: str, turns: list[tuple[str, "RequestRecord"]]) -> str:
    if not turns:
        return text
    earlier = "; ".join(f'{alias}: "{r.text[:300]}"' for alias, r in turns)
    return f"{text}\n(Earlier questions in this conversation: {earlier})"


class StageTiming(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    duration_ms: float


class RequestRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    user_id: str
    text: str
    files: tuple[str, ...] = ()
    state: RequestState
    created_at: datetime
    updated_at: datetime
    planner: str | None = None
    plan: ExecutionPlan | None = None
    routing: tuple[RoutingDecision, ...] = ()
    unrouted: tuple[UnroutedTask, ...] = ()
    unsupported: tuple[str, ...] = ()
    authorization: PlanAuthorization | None = None
    report: ExecutionReport | None = None
    error: ErrorInfo | None = None
    summary: str = ""
    stages: tuple[StageTiming, ...] = ()
    executions: int = 0
    replans: tuple[AttemptSummary, ...] = ()  # failed attempts that were re-planned, oldest first
    context: tuple[str, ...] = ()     # earlier requests of this conversation, oldest first (R1, R2, ...)
    answer: Answer | None = None      # grounded answer written from the tool results (see answers.py)
    answer_note: str = ""              # why there is no answer, when one was expected
    receipt: Receipt | None = None    # signed, hash-chained proof of this run (see receipts.py)
    code: CodeAnswer | None = None    # a code answer (see coding.py); its run, if any, is the plan

    @property
    def pending_approvals(self) -> dict[str, str]:
        if self.report is None:
            return {}
        return {
            r.task_id: r.approval_id
            for r in self.report.task_results
            if r.status == TaskStatus.AWAITING_APPROVAL and r.approval_id
        }

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["pending_approvals"] = self.pending_approvals
        data["requires_review"] = bool(self.report and self.report.requires_review)
        return data


class RequestStore:
    """In-memory request records, with (user, idempotency key) → request lookup."""

    def __init__(self) -> None:
        self._records: dict[str, RequestRecord] = {}
        self._keys: dict[tuple[str, str], str] = {}
        self.lock = asyncio.Lock()

    def get(self, request_id: str) -> RequestRecord | None:
        return self._records.get(request_id)

    def put(self, record: RequestRecord) -> None:
        self._records[record.request_id] = record

    def by_key(self, user_id: str, key: str) -> RequestRecord | None:
        rid = self._keys.get((user_id, key))
        return self._records.get(rid) if rid else None

    def bind_key(self, user_id: str, key: str, request_id: str) -> None:
        self._keys[(user_id, key)] = request_id


async def register_default_tools(
    registry: ToolRegistry, settings: Settings, *, search_backend: SearchBackend | None = None
) -> None:
    """File processing and Python execution always; web search only with a real backend —
    registering one that returns nothing would present 'no results' as an answer.
    ``search_backend`` overrides ``settings.search_backend``."""
    root = settings.file_tool_root
    root.mkdir(parents=True, exist_ok=True)
    await registry.register(FileProcessingTool(root), replace_existing=True)
    python_tool = build_python_tool(
        sandbox=settings.python_sandbox, wasm_dir=settings.python_wasm_dir,
        timeout_s=settings.python_exec_timeout_s, memory_mb=settings.python_memory_mb,
    )
    if python_tool.isolation != IsolationLevel.WASM:
        _log.warning("python_exec runs in a plain subprocess, which is not a security sandbox; "
                     "see scripts/fetch_python_wasm.py and ORCH_PYTHON_SANDBOX")
    await registry.register(python_tool, replace_existing=True)
    if search_backend is None and settings.search_backend == "anthropic":
        import anthropic

        from orchestrator.tools.anthropic_search import AnthropicSearchBackend

        key = settings.api_key_for("anthropic")
        client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        search_backend = AnthropicSearchBackend(
            client, model=settings.effective_search_model, max_uses=settings.search_max_uses
        )
    elif search_backend is None and settings.search_backend == "gemini":
        from orchestrator.routing.gemini_planner import make_gemini_client
        from orchestrator.tools.gemini_search import GeminiSearchBackend

        search_backend = GeminiSearchBackend(
            make_gemini_client(settings.api_key_for("gemini")), model=settings.effective_search_model
        )
    elif search_backend is None and settings.search_backend == "tavily":
        from orchestrator.tools.tavily_search import TavilySearchBackend

        search_backend = TavilySearchBackend(search_depth=settings.tavily_search_depth)
    if search_backend is not None:
        await registry.register(WebSearchTool(search_backend), replace_existing=True)


def _now() -> datetime:
    return datetime.now(UTC)


def _state_for(report: ExecutionReport) -> RequestState:
    return {
        ExecutionStatus.SUCCEEDED: RequestState.COMPLETED,
        ExecutionStatus.PARTIAL: RequestState.COMPLETED,
        ExecutionStatus.AWAITING_APPROVAL: RequestState.AWAITING_APPROVAL,
        ExecutionStatus.FAILED: RequestState.FAILED,
    }[report.status]


def build_summary(record: RequestRecord) -> str:
    """Deterministic plain-text summary of a request's outcome."""
    lines = [f"Request {record.request_id}: {record.state.value}."]
    if record.replans:
        lines.append(f"Re-planned {len(record.replans)} time(s) after plan errors.")
    if record.error is not None:
        lines.append(f"Reason: {record.error.message} ({record.error.code}).")
    report = record.report
    if report is not None:
        lines.append(f"Execution status: {report.status.value}.")
        for r in report.task_results:
            where = f"{r.tool_id}.{r.operation}" if r.tool_id else "unbound"
            timing = f", {r.duration_ms:.0f} ms" if r.duration_ms is not None else ""
            extra = " (reused result)" if r.deduplicated else ""
            line = f"- {r.task_id} [{where}]: {r.status.value}{timing}{extra}"
            if r.error is not None and r.status != TaskStatus.SUCCEEDED:
                line += f" — {r.error.message}"
            lines.append(line)
        escalated = [c for c in report.conflicts if c.escalated]
        if report.conflicts:
            lines.append(f"Conflicts: {len(report.conflicts)} detected, "
                         f"{sum(c.resolved for c in report.conflicts)} resolved, {len(escalated)} need review.")
        if report.uncertainty:
            lines.append("Caveats:")
            lines.extend(f"- {note}" for note in report.uncertainty)
    if record.answer is not None:
        a = record.answer
        lines.append(f"Answer: {a.supported} of {len(a.claims)} claim(s) checked against the evidence"
                     f"{'' if a.supported == len(a.claims) else ' (the rest are marked)'}.")
    elif record.answer_note:
        lines.append(f"No written answer: {record.answer_note}")
    if record.pending_approvals:
        lines.append("Waiting for approval: " + ", ".join(
            f"{task} (approval {aid})" for task, aid in sorted(record.pending_approvals.items())) + ".")
    return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        permissions: PermissionManager,
        approvals: ApprovalManager,
        audit: AuditLog | None = None,
        *,
        scheduler: DagScheduler | None = None,
        resolver: ConflictResolver | None = None,
        settings: Settings | None = None,
        store: RequestStore | None = None,
        idempotency_backend: IdempotencyBackend | None = None,
        composer: AnswerComposer | None = None,
        receipts: ReceiptIssuer | None = None,
        code_writer: CodeWriter | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.composer = composer
        self.receipts = receipts
        self.code_writer = code_writer
        self.registry = registry
        self.permissions = permissions
        self.approvals = approvals
        self.audit = audit or AuditLog()
        self.routing = RoutingService(registry, planner, permissions, self.audit)
        self.scheduler = scheduler or DagScheduler(
            ExecutionEngine.from_settings(self.settings, registry, permissions, approvals, self.audit,
                                          idempotency_backend=idempotency_backend),
            self.audit,
        )
        self.resolver = resolver or ConflictResolver(registry, self.audit)
        self.store = store if store is not None else RequestStore()

    @classmethod
    def build(
        cls,
        settings: Settings,
        registry: ToolRegistry,
        permissions: PermissionManager,
        approvals: ApprovalManager,
        audit: AuditLog | None = None,
        *,
        planner: Planner | None = None,
        store: RequestStore | None = None,
        idempotency_backend: IdempotencyBackend | None = None,
        receipt_chain: ReceiptChain | None = None,
    ) -> "Orchestrator":
        composer = build_composer(settings) if planner is None else None
        code_writer = build_code_writer(settings) if planner is None else None
        receipts = None
        if settings.receipts != "off":
            signer = Signer(settings.receipt_key_path) if settings.receipt_key_path else None
            receipts = ReceiptIssuer(receipt_chain, signer, policy_grants=lambda: permissions.policy.grants,
                                     approver=_approver(approvals))
        return cls(registry, planner or build_planner(settings, audit), permissions, approvals, audit,
                   settings=settings, store=store, idempotency_backend=idempotency_backend, composer=composer,
                   receipts=receipts, code_writer=code_writer)

    # ------------------------------------------------------------------ queries

    def get(self, request_id: str, principal: Principal) -> RequestRecord:
        record = self.store.get(request_id)
        if record is None or (record.user_id != principal.user_id and self.settings.admin_role not in principal.roles):
            raise RequestNotFoundError(f"request {request_id!r} not found")  # never confirm others' requests
        return record

    # ------------------------------------------------------------------ submit

    async def submit(
        self,
        text: str,
        principal: Principal,
        *,
        files: tuple[str, ...] = (),
        idempotency_key: str | None = None,
        allow_partial: bool = False,
        request_id: str | None = None,
        context: Sequence[str] = (),
        mode: str = "auto",
    ) -> RequestRecord:
        """``request_id`` lets a caller choose the id up front (the streaming API subscribes to its
        progress before the request starts); by default one is generated."""
        ids = {"request_id": request_id} if request_id else {}
        context = tuple(dict.fromkeys(context))
        if len(context) > MAX_CONTEXT:
            raise ValueError(f"at most {MAX_CONTEXT} earlier requests can be used as context")
        turns = self._turns(context, principal.user_id)  # raises before anything is recorded
        request = PlanningRequest(text=text, files=tuple(files), history=_history(turns), **ids)  # stage 1
        async with self.store.lock:
            if idempotency_key:
                existing = self.store.by_key(principal.user_id, idempotency_key)
                if existing is not None:
                    self.audit.record("request.duplicate", request_id=existing.request_id, user_id=principal.user_id)
                    return existing
            now = _now()
            record = RequestRecord(
                request_id=request.request_id, user_id=principal.user_id, text=text, files=request.files,
                state=RequestState.RUNNING, created_at=now, updated_at=now, context=context,
            )
            self.store.put(record)
            if idempotency_key:
                self.store.bind_key(principal.user_id, idempotency_key, record.request_id)
        self.audit.record("request.received", request_id=record.request_id, user_id=principal.user_id,
                          files=list(request.files), context=list(context))
        started = time.perf_counter()
        stages: list[StageTiming] = []

        if mode == "code" or (mode == "auto" and self.code_writer is not None and wants_code(text)):
            return await self._code(record, principal, started, stages, turns, idempotency_key)

        t = time.perf_counter()
        try:
            outcome = await self.routing.plan_and_route(request, principal, allow_partial=allow_partial)
            if outcome.routed.plan is None:
                raise NoMatchingToolError(
                    "nothing in this request could be routed to a permitted, available tool",
                    details={"unrouted": [u.model_dump() for u in outcome.routed.unrouted],
                             "unsupported": list(outcome.routed.unsupported)},
                )
        except OrchestratorError as exc:
            stages.append(StageTiming(stage="plan_route_authorize", duration_ms=self._ms(t)))
            return self._finish(record, started, stages, state=RequestState.REJECTED,
                                error=ErrorInfo.from_exception(exc))
        except Exception as exc:  # a bug: record it, never leave the request stuck in RUNNING
            stages.append(StageTiming(stage="plan_route_authorize", duration_ms=self._ms(t)))
            self._finish(record, started, stages, state=RequestState.FAILED, error=ErrorInfo.from_exception(exc))
            raise
        stages.append(StageTiming(stage="plan_route_authorize", duration_ms=self._ms(t)))
        record = record.model_copy(update={
            "planner": outcome.proposal.planner,
            "plan": outcome.routed.plan,
            "routing": outcome.routed.decisions,
            "unrouted": outcome.routed.unrouted,
            "unsupported": outcome.routed.unsupported,
            "authorization": outcome.authorization,
        })
        return await self._execute(record, principal, started, stages, idempotency_key=idempotency_key,
                                   request=request, allow_partial=allow_partial)

    # ------------------------------------------------------------------ resume

    async def resume(self, request_id: str, principal: Principal, *, idempotency_key: str | None = None) -> RequestRecord:
        async with self.store.lock:
            record = self.get(request_id, principal)
            if record.user_id != principal.user_id:
                raise RequestNotFoundError(f"request {request_id!r} not found")  # admins may view, not act
            if record.state != RequestState.AWAITING_APPROVAL:
                raise InvalidRequestStateError(
                    f"request is {record.state.value}; only requests awaiting approval can be resumed",
                    details={"state": record.state.value},
                )
            undecided = [t for t, aid in record.pending_approvals.items()
                         if self.approvals.get(aid).status == ApprovalStatus.PENDING]
            if undecided:
                raise ApprovalRequiredError(
                    "some approvals are still pending",
                    details={"tasks": undecided, "status": "pending"},
                )
            record = record.model_copy(update={"state": RequestState.RUNNING, "updated_at": _now()})
            self.store.put(record)
        self.audit.record("request.resumed", request_id=request_id, user_id=principal.user_id)
        return await self._execute(record, principal, time.perf_counter(), [], idempotency_key=idempotency_key,
                                   resume=True)

    # ------------------------------------------------------------------ code answers

    async def _code(self, record: RequestRecord, principal: Principal, started: float, stages: list[StageTiming],
                    turns: list[tuple[str, RequestRecord]], idempotency_key: str | None) -> RequestRecord:
        """A request whose answer is code (see coding.py). Runnable Python then runs as an ordinary
        one-step plan: routed, permission-checked, executed, audited and receipted."""
        t = time.perf_counter()
        if self.code_writer is None:
            stages.append(StageTiming(stage="write_code", duration_ms=self._ms(t)))
            return self._finish(record, started, stages, state=RequestState.REJECTED, error=ErrorInfo(
                code="configuration_error",
                message="Code answers need a Groq, Ollama or OpenAI-compatible planner."))
        self.audit.record("code.writing", request_id=record.request_id, user_id=principal.user_id,
                          writer=self.code_writer.name)
        try:
            code = await self.code_writer.write(record.text, latest_code([r for _, r in turns]))
        except OrchestratorError as exc:
            stages.append(StageTiming(stage="write_code", duration_ms=self._ms(t)))
            return self._finish(record, started, stages, state=RequestState.REJECTED, error=ErrorInfo.from_exception(exc))
        except Exception as exc:  # provider errors: reported, never leaving the request RUNNING
            stages.append(StageTiming(stage="write_code", duration_ms=self._ms(t)))
            return self._finish(record, started, stages, state=RequestState.REJECTED, error=ErrorInfo(
                code="code_writer_failed", message=f"The code could not be written ({type(exc).__name__})."))
        stages.append(StageTiming(stage="write_code", duration_ms=self._ms(t)))
        self.audit.record("code.written", request_id=record.request_id, user_id=principal.user_id,
                          language=code.language, runnable=code.runnable, syntax_ok=code.syntax_ok,
                          lines=code.code.count("\n") + 1)
        record = record.model_copy(update={"code": code, "planner": self.code_writer.name})
        if not (code.is_python and code.runnable and code.syntax_ok):
            return self._finish(record, started, stages, state=RequestState.COMPLETED)

        proposal = build_proposal(request_id=record.request_id, planner=self.code_writer.name, rationale="run the written code",
                                  tasks=[{"task_id": "run_code", "description": f"Run {code.filename} in the sandbox",
                                          "capability": "code.python.execute",
                                          "inputs": {"code": code.code, "inputs": {}}}])
        t = time.perf_counter()
        try:
            outcome = self.routing.route_proposal(proposal, principal, request_id=record.request_id)
        except OrchestratorError as exc:
            stages.append(StageTiming(stage="route_authorize", duration_ms=self._ms(t)))
            denied = isinstance(exc, (PermissionDeniedError, NoMatchingToolError))
            note = "Not run: you don't have permission to run code." if denied else f"Not run: {exc.message}"
            record = record.model_copy(update={"code": code.model_copy(update={"run_note": note})})
            return self._finish(record, started, stages, state=RequestState.COMPLETED)
        stages.append(StageTiming(stage="route_authorize", duration_ms=self._ms(t)))
        record = record.model_copy(update={
            "plan": outcome.routed.plan, "routing": outcome.routed.decisions, "unrouted": outcome.routed.unrouted,
            "unsupported": outcome.routed.unsupported, "authorization": outcome.authorization,
        })
        return await self._execute(record, principal, started, stages, idempotency_key=idempotency_key)

    # ------------------------------------------------------------------ replay

    async def replay(self, request_id: str, principal: Principal) -> "ReplayReport":
        """Run a finished request's stored plan again and compare every step (see replay.py).
        Only the requester may replay: admins may view others' requests, not act on them."""
        from orchestrator.replay import replay_request

        record = self.get(request_id, principal)
        if record.user_id != principal.user_id:
            raise RequestNotFoundError(f"request {request_id!r} not found")
        if record.plan is None or record.report is None or record.state not in (RequestState.COMPLETED,
                                                                                 RequestState.FAILED):
            raise InvalidRequestStateError(
                f"request is {record.state.value}; only requests that ran to the end can be replayed",
                details={"state": record.state.value},
            )
        self.audit.record("request.replay_started", request_id=request_id, user_id=principal.user_id)
        report = await replay_request(self, record, principal)
        self.audit.record("request.replayed", request_id=request_id, user_id=principal.user_id,
                          replay_id=report.replay_id, verdict=report.verdict, counts=report.counts,
                          policy_changed=report.policy_changed, receipt_matches_record=report.receipt_matches_record)
        return report

    # ------------------------------------------------------------------ internals

    async def _execute(self, record: RequestRecord, principal: Principal, started: float,
                       stages: list[StageTiming], *, idempotency_key: str | None, resume: bool = False,
                       request: PlanningRequest | None = None, allow_partial: bool = False) -> RequestRecord:
        prior = {r.task_id: r for r in record.report.task_results} if resume and record.report else None
        report = await self._run(record, principal, stages, idempotency_key=idempotency_key, prior=prior)
        runs = 1
        if not resume and request is not None:
            record, report, extra_runs = await self._replan(record, report, request, principal, stages,
                                                            idempotency_key=idempotency_key, allow_partial=allow_partial)
            runs += extra_runs
        state = _state_for(report)
        if record.code is not None and state == RequestState.FAILED:
            state = RequestState.COMPLETED  # the code was delivered; its failed run is shown, not hidden
        answer_fields = await self._answer(record, report, state, stages)
        return self._finish(record, started, stages, state=state, report=report,
                            executions=record.executions + runs, **answer_fields)

    async def _answer(self, record: RequestRecord, report: ExecutionReport, state: RequestState,
                      stages: list[StageTiming]) -> dict[str, Any]:
        """Stage 11: a grounded answer from the results. Never fails the request: a composer error
        leaves the deterministic summary as the response and says why."""
        if self.composer is None or state != RequestState.COMPLETED or record.code is not None:
            return {}
        t = time.perf_counter()
        self.audit.record("answer.started", request_id=record.request_id, composer=self.composer.name)
        try:
            turns = self._turns(record.context, record.user_id)
            answer = await compose_answer(self.composer, _question(record.text, turns), record.plan, report,
                                          extra=earlier_evidence(record.plan, turns))
        except Exception as exc:  # the answer is an extra; the request already succeeded
            stages.append(StageTiming(stage="answer", duration_ms=self._ms(t)))
            self.audit.record("answer.failed", request_id=record.request_id, user_id=record.user_id,
                              composer=self.composer.name, error=type(exc).__name__)
            return {"answer": None, "answer_note": f"the answer could not be written ({type(exc).__name__})."}
        stages.append(StageTiming(stage="answer", duration_ms=self._ms(t)))
        if answer is None:
            return {}
        self.audit.record(
            "answer.composed", request_id=record.request_id, user_id=record.user_id, composer=answer.composer,
            model=answer.model, evidence=len(answer.evidence), claims=len(answer.claims), supported=answer.supported,
            statuses={s: sum(c.status == s for c in answer.claims) for s in ("supported", "unverified", "uncited")},
        )
        return {"answer": answer, "answer_note": ""}

    async def _run(self, record: RequestRecord, principal: Principal, stages: list[StageTiming], *,
                   idempotency_key: str | None, prior: dict[str, Any] | None, label: str = "") -> ExecutionReport:
        """Execute ``record.plan`` and resolve conflicts (stages 7-10)."""
        assert record.plan is not None
        t = time.perf_counter()
        try:
            report = await self.scheduler.execute(
                record.plan, principal=principal, idempotency_key=idempotency_key or record.request_id,
                approvals=record.pending_approvals, deadline_s=self.settings.execution_deadline_s, prior=prior,
                context=self._context_outputs(record),
            )
        except BaseException:
            # Never leave a record stuck in RUNNING (e.g. the caller was cancelled).
            self.store.put(record.model_copy(update={"state": RequestState.FAILED, "updated_at": _now(),
                                                     "error": ErrorInfo(code="execution_cancelled",
                                                                        message="execution was interrupted")}))
            raise
        stages.append(StageTiming(stage=f"execute{label}", duration_ms=self._ms(t)))

        t = time.perf_counter()
        report = self.resolver.resolve(record.plan, report)
        routing_notes = tuple(
            [f"Not attempted: {u.task_id} ({u.reason})." for u in record.unrouted]
            + [f"Not supported by any available tool: {s}." for s in record.unsupported]
        )
        if routing_notes:
            report = report.model_copy(update={"uncertainty": (*routing_notes, *report.uncertainty)})
        stages.append(StageTiming(stage=f"resolve{label}", duration_ms=self._ms(t)))
        return report

    async def _replan(self, record: RequestRecord, report: ExecutionReport, request: PlanningRequest,
                      principal: Principal, stages: list[StageTiming], *, idempotency_key: str | None,
                      allow_partial: bool) -> tuple[RequestRecord, ExecutionReport, int]:
        """Re-plan after plan-caused failures, at most ``max_replans`` times (see ``orchestrator.repair``).
        Each new plan is routed, authorized and executed exactly like the first; steps the new plan
        repeats unchanged reuse their earlier results."""
        attempts: list[AttemptSummary] = []
        notes: list[str] = []
        runs = 0
        for n in range(1, self.settings.max_replans + 1):
            if not self.routing.supports_feedback or record.plan is None:
                break
            failed = repairable_failures(record.plan, report, self.registry)
            if not failed:
                break
            attempts.append(AttemptSummary(
                plan_id=record.plan.plan_id,
                failed={r.task_id: f"{r.error.code}: {r.error.message}" for r in failed if r.error},
            ))
            self.audit.record("request.replanning", request_id=record.request_id, attempt=n,
                              failed={r.task_id: r.error.code for r in failed if r.error})
            t = time.perf_counter()
            try:
                outcome = await self.routing.plan_and_route(
                    request, principal, allow_partial=allow_partial,
                    feedback=build_feedback(record.plan, report, failed),
                )
            except OrchestratorError as exc:
                stages.append(StageTiming(stage=f"replan_{n}", duration_ms=self._ms(t)))
                notes.append(f"Re-planning after the failure did not succeed ({exc.code}: {exc.message}).")
                self.audit.record("request.replan_failed", request_id=record.request_id, attempt=n, error=exc.code)
                break
            stages.append(StageTiming(stage=f"replan_{n}", duration_ms=self._ms(t)))
            if outcome.routed.plan is None:
                notes.append("Re-planning produced no runnable plan.")
                break
            prior = reusable_results(record.plan, report, outcome.routed.plan)
            record = record.model_copy(update={
                "plan": outcome.routed.plan,
                "routing": outcome.routed.decisions,
                "unrouted": outcome.routed.unrouted,
                "unsupported": outcome.routed.unsupported,
                "authorization": outcome.authorization,
            })
            report = await self._run(record, principal, stages, idempotency_key=idempotency_key,
                                     prior=prior or None, label=f"_{n}")
            runs += 1
        if attempts:
            failed_text = "; ".join(f"{tid} ({msg})" for a in attempts for tid, msg in a.failed.items())
            notes.insert(0, f"The plan was revised {len(attempts)} time(s) after failures: {failed_text}.")
            report = report.model_copy(update={"uncertainty": (*notes, *report.uncertainty)})
            record = record.model_copy(update={"replans": tuple(attempts)})
        return record, report, runs

    def _finish(self, record: RequestRecord, started: float, stages: list[StageTiming], *,
                state: RequestState, **fields: Any) -> RequestRecord:
        stages.append(StageTiming(stage="total", duration_ms=self._ms(started)))
        record = record.model_copy(update={"state": state, "updated_at": _now(),
                                           "stages": (*record.stages, *stages), **fields})
        record = record.model_copy(update={"summary": build_summary(record)})
        if self.receipts is not None and state not in (RequestState.RUNNING, RequestState.AWAITING_APPROVAL):
            receipt = self.receipts.issue(record)
            record = record.model_copy(update={"receipt": receipt})
            self.audit.record("receipt.issued", request_id=record.request_id, sequence=receipt.sequence,
                              receipt_sha256=receipt.receipt_sha256, signed=receipt.signature is not None)
        self.store.put(record)
        self.audit.record(
            "request.finished", request_id=record.request_id, user_id=record.user_id, state=state.value,
            duration_ms=stages[-1].duration_ms,
            error=record.error.code if record.error else None,
            requires_review=bool(record.report and record.report.requires_review),
        )
        return record

    # ------------------------------------------------------------------ conversation memory

    def _turns(self, context: Sequence[str], user_id: str) -> list[tuple[str, RequestRecord]]:
        """Earlier requests as (alias, record). Only the user's own completed requests: anyone else's
        is reported as not found (the API never confirms that another user's request exists), and
        admins get no exception here — their view rights do not extend to reusing others' results."""
        turns = []
        for n, request_id in enumerate(context, start=1):
            earlier = self.store.get(request_id)
            if earlier is None or earlier.user_id != user_id:
                raise RequestNotFoundError(f"request {request_id!r} not found")
            if earlier.state != RequestState.COMPLETED or earlier.report is None:
                raise InvalidRequestStateError(
                    f"request {request_id!r} has no results to build on ({earlier.state.value})",
                    details={"request_id": request_id, "state": earlier.state.value},
                )
            turns.append((f"R{n}", earlier))
        return turns

    def _context_outputs(self, record: RequestRecord) -> dict[str, dict[str, Any]]:
        return {alias: _outputs(earlier) for alias, earlier in self._turns(record.context, record.user_id)}

    @staticmethod
    def _ms(since: float) -> float:
        return round((time.perf_counter() - since) * 1000, 3)
