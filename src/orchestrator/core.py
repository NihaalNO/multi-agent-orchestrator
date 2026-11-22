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
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.config import Settings
from orchestrator.contracts import (
    ApprovalStatus,
    ErrorInfo,
    ExecutionPlan,
    ExecutionReport,
    ExecutionStatus,
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
    RequestNotFoundError,
)
from orchestrator.execution import DagScheduler, ExecutionEngine
from orchestrator.repair import AttemptSummary, build_feedback, repairable_failures, reusable_results
from orchestrator.execution.coordination import IdempotencyBackend
from orchestrator.observability.audit import AuditLog
from orchestrator.permissions import ApprovalManager, PermissionManager, PlanAuthorization
from orchestrator.registry.registry import ToolRegistry
from orchestrator.resolution import ConflictResolver
from orchestrator.routing import Planner, RoutingService, build_planner
from orchestrator.tools import FileProcessingTool, PythonExecTool, SearchBackend, WebSearchTool


class RequestState(StrEnum):
    RUNNING = "running"
    REJECTED = "rejected"            # could not be planned, routed or authorized; nothing ran
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"          # everything, or part of it, succeeded (see report.status)
    FAILED = "failed"                # execution ran but nothing succeeded


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
    await registry.register(PythonExecTool(timeout_s=settings.python_exec_timeout_s), replace_existing=True)
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
    ) -> None:
        self.settings = settings or Settings()
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
    ) -> "Orchestrator":
        return cls(registry, planner or build_planner(settings, audit), permissions, approvals, audit,
                   settings=settings, store=store, idempotency_backend=idempotency_backend)

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
    ) -> RequestRecord:
        request = PlanningRequest(text=text, files=tuple(files))  # stage 1: validates length etc.
        async with self.store.lock:
            if idempotency_key:
                existing = self.store.by_key(principal.user_id, idempotency_key)
                if existing is not None:
                    self.audit.record("request.duplicate", request_id=existing.request_id, user_id=principal.user_id)
                    return existing
            now = _now()
            record = RequestRecord(
                request_id=request.request_id, user_id=principal.user_id, text=text, files=request.files,
                state=RequestState.RUNNING, created_at=now, updated_at=now,
            )
            self.store.put(record)
            if idempotency_key:
                self.store.bind_key(principal.user_id, idempotency_key, record.request_id)
        self.audit.record("request.received", request_id=record.request_id, user_id=principal.user_id,
                          files=list(request.files))
        started = time.perf_counter()
        stages: list[StageTiming] = []

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
        return self._finish(record, started, stages, state=_state_for(report), report=report,
                            executions=record.executions + runs)

    async def _run(self, record: RequestRecord, principal: Principal, stages: list[StageTiming], *,
                   idempotency_key: str | None, prior: dict[str, Any] | None, label: str = "") -> ExecutionReport:
        """Execute ``record.plan`` and resolve conflicts (stages 7-10)."""
        assert record.plan is not None
        t = time.perf_counter()
        try:
            report = await self.scheduler.execute(
                record.plan, principal=principal, idempotency_key=idempotency_key or record.request_id,
                approvals=record.pending_approvals, deadline_s=self.settings.execution_deadline_s, prior=prior,
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
        self.store.put(record)
        self.audit.record(
            "request.finished", request_id=record.request_id, user_id=record.user_id, state=state.value,
            duration_ms=stages[-1].duration_ms,
            error=record.error.code if record.error else None,
            requires_review=bool(record.report and record.report.requires_review),
        )
        return record

    @staticmethod
    def _ms(since: float) -> float:
        return round((time.perf_counter() - since) * 1000, 3)
