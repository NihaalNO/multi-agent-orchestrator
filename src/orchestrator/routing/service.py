"""Planning + routing + authorization pipeline (lifecycle stages 2–6) and planner construction."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from orchestrator.config import Settings
from orchestrator.contracts import PlanningRequest, PlanProposal, Principal, RoutedPlan
from orchestrator.errors import ConfigurationError, NoMatchingToolError, PermissionDeniedError, PlanOutputError
from orchestrator.observability.audit import AuditLog
from orchestrator.permissions.manager import PermissionManager, PlanAuthorization
from orchestrator.registry.registry import ToolRegistry
from orchestrator.routing.planner import Planner, RuleBasedPlanner, ToolCatalog
from orchestrator.routing.router import CapabilityRouter


_CSV_PATH = re.compile(r"[\w\-./]+\.csv\b", re.IGNORECASE)


def build_planner(settings: Settings, audit: AuditLog | None = None) -> Planner:
    provider = (settings.llm_provider or "").lower()
    if provider == "rule_based":
        return RuleBasedPlanner()
    key = settings.api_key_for(provider)
    if provider == "anthropic":
        import anthropic

        from orchestrator.routing.anthropic_planner import AnthropicPlanner

        client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        return AnthropicPlanner(client, model=settings.planner_model, effort=settings.llm_effort, audit=audit)
    if provider == "gemini":
        from orchestrator.routing.gemini_planner import GeminiPlanner, make_gemini_client

        return GeminiPlanner(make_gemini_client(key), model=settings.planner_model, audit=audit)
    from orchestrator.routing.openai_compat_planner import PROVIDERS, OpenAICompatPlanner, make_openai_compat_client

    if provider in PROVIDERS:
        profile = PROVIDERS[provider]
        base_url = profile.resolve_base_url(settings.llm_base_url)
        client = make_openai_compat_client(profile, api_key=key, base_url=base_url)
        return OpenAICompatPlanner(
            profile, client, model=settings.planner_model, base_url=base_url,
            json_mode=None if settings.llm_json_mode == "auto" else settings.llm_json_mode,
            max_output_tokens=settings.llm_max_output_tokens, audit=audit,
        )
    raise ConfigurationError(
        f"unknown llm_provider {settings.llm_provider!r}; expected one of "
        "'anthropic', 'gemini', 'groq', 'ollama', 'openai', 'rule_based'"
    )


class RoutingOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal: PlanProposal
    routed: RoutedPlan
    authorization: PlanAuthorization


class RoutingService:
    """Plans a request for a principal, routes it to permitted tools and authorizes the result.

    Permission checks happen three times, each deterministic and outside the LLM:
      1. the planner only sees operations the principal could ever use;
      2. the router only picks candidates permitted for each task's actual inputs;
      3. the bound plan is authorized task by task (audited), and refused if anything is denied.
    The execution engine checks once more with fully resolved inputs (Phase 05).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        permissions: PermissionManager,
        audit: AuditLog | None = None,
    ) -> None:
        self._registry = registry
        self._planner = planner
        self._permissions = permissions
        self._audit = audit or AuditLog()

    def route_proposal(self, proposal: PlanProposal, principal: Principal, *, request_id: str,
                       allow_partial: bool = False) -> "RoutingOutcome":
        """Route and authorize a plan built by code rather than the planner (for example running
        generated code): the same router, candidate filter and authorization as a planned request."""
        self._audit.record("planner.proposal", request_id=request_id, user_id=principal.user_id,
                           planner=proposal.planner, tasks=[t.task_id for t in proposal.plan.tasks] if proposal.plan else [],
                           unsupported=list(proposal.unsupported))
        router = CapabilityRouter(
            self._registry, self._audit, candidate_filter=self._permissions.candidate_filter(principal)
        )
        routed = router.route(proposal, allow_partial=allow_partial)
        authorization = self._permissions.authorize_plan(principal, routed, request_id=request_id)
        if not authorization.allowed:
            raise PermissionDeniedError(
                "plan contains operations the user is not permitted to perform",
                details={"denied": {t: authorization.decisions[t].reason for t in authorization.denied}},
            )
        return RoutingOutcome(proposal=proposal, routed=routed, authorization=authorization)

    def _file_columns(self, request: PlanningRequest, principal: Principal) -> dict[str, tuple[str, ...]]:
        """Column names of the CSVs this request refers to (listed files, plus ``.csv`` paths named
        in its text) that the principal may read with the file tool. Header only — no data values —
        so the planner can use real column names instead of guessing them."""
        if "file_processing" not in self._registry:
            return {}
        tool = self._registry.get("file_processing").adapter
        peek = getattr(tool, "peek_columns", None)
        if peek is None:
            return {}
        candidates = list(dict.fromkeys([*request.files, *_CSV_PATH.findall(request.text)]))[:20]
        out: dict[str, tuple[str, ...]] = {}
        for path in candidates:
            if not path.lower().endswith(".csv"):
                continue
            allowed = self._permissions.check(principal, "file_processing", "read_csv", {"path": path},
                                              request_id=request.request_id, audit=False).allowed
            if not allowed:
                continue  # never reveal anything about files the user may not read
            cols = peek(path)
            if cols:
                out[path] = tuple(cols[:200])
        return out

    async def _call_planner(self, request: PlanningRequest, catalog: ToolCatalog, feedback: str | None):
        if feedback and self.supports_feedback:
            return await self._planner.plan(request, catalog, feedback=feedback)  # type: ignore[call-arg]
        return await self._planner.plan(request, catalog)

    @property
    def supports_feedback(self) -> bool:
        return bool(getattr(self._planner, "supports_feedback", False))

    async def plan_and_route(
        self, request: PlanningRequest, principal: Principal, *, allow_partial: bool = False,
        feedback: str | None = None,
    ) -> RoutingOutcome:
        """``feedback`` describes a failed earlier attempt (see ``orchestrator.repair``); only
        planners with ``supports_feedback`` receive it."""
        catalog = ToolCatalog.from_registry(
            self._registry, include=lambda spec, op: self._permissions.operation_visible(principal, spec, op)
        )
        columns = self._file_columns(request, principal)
        if columns:
            request = request.model_copy(update={"file_columns": columns})
            self._audit.record("planner.file_hints", request_id=request.request_id, user_id=principal.user_id,
                               files=sorted(columns))
        try:
            proposal = await self._call_planner(request, catalog, feedback)
        except PlanOutputError as exc:
            # LLM output is not deterministic: a plan that could not be read (bad JSON in a task's
            # inputs, say) is usually fine on a second try once the model is told what was wrong.
            if not self.supports_feedback:
                raise
            self._audit.record("planner.retry", request_id=request.request_id, user_id=principal.user_id,
                               reason="unreadable plan output", error=exc.message)
            note = (f"Your previous plan could not be read: {exc.message}. Return a complete, corrected "
                    "plan; every inputs_json must be one valid JSON object.")
            proposal = await self._call_planner(request, catalog, f"{feedback}\n\n{note}" if feedback else note)
        self._audit.record(
            "planner.proposal",
            request_id=request.request_id,
            user_id=principal.user_id,
            planner=proposal.planner,
            tasks=[t.task_id for t in proposal.plan.tasks] if proposal.plan else [],
            unsupported=list(proposal.unsupported),
        )
        router = CapabilityRouter(
            self._registry, self._audit, candidate_filter=self._permissions.candidate_filter(principal)
        )
        try:
            routed = router.route(proposal, allow_partial=allow_partial)
        except NoMatchingToolError as exc:
            # Tools this principal may not use were hidden from the planner. Say that the gap may
            # be a permissions one, without revealing which tools exist.
            restricted = len(catalog.entries) < len(ToolCatalog.from_registry(self._registry).entries)
            if not restricted:
                raise
            raise NoMatchingToolError(
                "no available tool that you are permitted to use can handle part of this request",
                details={**exc.details, "restricted": True},
            ) from exc
        authorization = self._permissions.authorize_plan(principal, routed, request_id=request.request_id)
        if not authorization.allowed:
            # Defence in depth: the router's filter should already have prevented this.
            raise PermissionDeniedError(
                "plan contains operations the user is not permitted to perform",
                details={"denied": {t: authorization.decisions[t].reason for t in authorization.denied}},
            )
        return RoutingOutcome(proposal=proposal, routed=routed, authorization=authorization)
