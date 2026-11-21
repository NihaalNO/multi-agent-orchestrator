"""Planning + routing pipeline (lifecycle stages 2–5) and planner construction."""

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.contracts import PlanningRequest, PlanProposal, RoutedPlan
from orchestrator.errors import ConfigurationError
from orchestrator.observability.audit import AuditLog
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


class RoutingService:
    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        router: CapabilityRouter | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._registry = registry
        self._planner = planner
        self._audit = audit or AuditLog()
        self._router = router or CapabilityRouter(registry, self._audit)

    async def plan_and_route(
        self, request: PlanningRequest, *, allow_partial: bool = False
    ) -> tuple[PlanProposal, RoutedPlan]:
        catalog = ToolCatalog.from_registry(self._registry)
        proposal = await self._planner.plan(request, catalog)
        self._audit.record(
            "planner.proposal",
            request_id=request.request_id,
            planner=proposal.planner,
            tasks=[t.task_id for t in proposal.plan.tasks] if proposal.plan else [],
            unsupported=list(proposal.unsupported),
        )
        routed = self._router.route(proposal, allow_partial=allow_partial)
        return proposal, routed
