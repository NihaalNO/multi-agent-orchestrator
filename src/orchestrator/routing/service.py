"""Planning + routing + authorization pipeline (lifecycle stages 2–6) and planner construction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from orchestrator.config import Settings
from orchestrator.contracts import PlanningRequest, PlanProposal, Principal, RoutedPlan
from orchestrator.errors import ConfigurationError, NoMatchingToolError, PermissionDeniedError
from orchestrator.observability.audit import AuditLog
from orchestrator.permissions.manager import PermissionManager, PlanAuthorization
from orchestrator.registry.registry import ToolRegistry
from orchestrator.routing.planner import Planner, RuleBasedPlanner, ToolCatalog
from orchestrator.routing.router import CapabilityRouter


def build_planner(settings: Settings, audit: AuditLog | None = None) -> Planner:
    provider = (settings.llm_provider or "").lower()
    if provider == "rule_based":
        return RuleBasedPlanner()
    if provider == "anthropic":
        import anthropic

        from orchestrator.routing.anthropic_planner import AnthropicPlanner

        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
        client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        return AnthropicPlanner(client, model=settings.llm_model, effort=settings.llm_effort, audit=audit)
    raise ConfigurationError(
        f"unknown llm_provider {settings.llm_provider!r}; expected 'anthropic' or 'rule_based'"
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

    async def plan_and_route(
        self, request: PlanningRequest, principal: Principal, *, allow_partial: bool = False
    ) -> RoutingOutcome:
        catalog = ToolCatalog.from_registry(
            self._registry, include=lambda spec, op: self._permissions.operation_visible(principal, spec, op)
        )
        proposal = await self._planner.plan(request, catalog)
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
