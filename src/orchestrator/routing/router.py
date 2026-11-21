"""Capability-based router.

Turns an untrusted ``PlanProposal`` into a ``RoutedPlan`` in which every task is bound to a
registered, available operation that really provides the task's capability.

For each task:
  1. If the planner proposed a binding, it is **re-verified** against the registry (tool
     exists, operation exists, operation declares the capability, tool is available, candidate
     filter allows it). A valid proposal is kept; an invalid one is overridden and the reason
     recorded — never silently trusted.
  2. Otherwise the highest-ranked available candidate is chosen. Ranking is deterministic:
     reliability (desc), cost, expected latency, then id.
  3. Inputs without ``$ref`` markers are validated against the operation's input schema now
     (fail fast). Inputs that reference upstream outputs are validated at execution time.

``candidate_filter`` is the hook through which the permission manager (Phase 04) removes
operations the principal may not use, so routing never picks an unauthorized tool when an
authorized one exists.
"""

from __future__ import annotations

from typing import Callable

from orchestrator.contracts import (
    CapabilityMatch,
    PlanProposal,
    RoutedPlan,
    RoutingDecision,
    TaskSpec,
    UnroutedTask,
    find_refs,
)
from orchestrator.errors import InputValidationError, NoMatchingToolError
from orchestrator.observability.audit import AuditLog
from orchestrator.registry.registry import ToolRegistry

CandidateFilter = Callable[[CapabilityMatch], bool]


def _label(m: CapabilityMatch) -> str:
    return f"{m.tool_id}.{m.operation}"


class CapabilityRouter:
    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditLog | None = None,
        *,
        candidate_filter: CandidateFilter | None = None,
    ) -> None:
        self._registry = registry
        self._audit = audit or AuditLog()
        self._filter = candidate_filter

    def candidates(self, capability: str) -> list[CapabilityMatch]:
        """Available, permitted candidates for a capability, best first."""
        return [
            m
            for m in self._registry.find_by_capability(capability, available_only=True)
            if self._filter is None or self._filter(m)
        ]

    def route(self, proposal: PlanProposal, *, allow_partial: bool = False) -> RoutedPlan:
        """Bind every task. Raises ``NoMatchingToolError`` if anything cannot be routed,
        unless ``allow_partial`` — then unroutable tasks and their dependents are dropped and
        reported in ``RoutedPlan.unrouted``."""
        plan = proposal.plan
        decisions: list[RoutingDecision] = []
        unrouted: list[UnroutedTask] = []

        if plan is not None:
            for task in plan.tasks:
                decision = self._decide(task)
                if isinstance(decision, UnroutedTask):
                    unrouted.append(decision)
                else:
                    decisions.append(decision)

        if (unrouted or proposal.unsupported or plan is None) and not allow_partial:
            self._audit.record(
                "routing.failed",
                request_id=proposal.request_id,
                unrouted=[u.model_dump() for u in unrouted],
                unsupported=list(proposal.unsupported),
            )
            raise NoMatchingToolError(
                "no available tool can handle part of this request",
                details={
                    "unrouted": [u.model_dump() for u in unrouted],
                    "unsupported": list(proposal.unsupported),
                },
            )

        dropped = {u.task_id for u in unrouted}
        remaining = plan.without(dropped) if plan is not None and dropped else plan
        if plan is not None:
            # Dependents of unroutable tasks are dropped with them; report those too.
            kept = {t.task_id for t in remaining.tasks} if remaining is not None else set()
            unrouted.extend(
                UnroutedTask(
                    task_id=t.task_id,
                    capability=t.capability,
                    reason="depends on a task that could not be routed",
                )
                for t in plan.tasks
                if t.task_id not in kept and t.task_id not in dropped
            )

        bound_plan = None
        if remaining is not None:
            by_id = {d.task_id: d for d in decisions}
            decisions = [by_id[t.task_id] for t in remaining.tasks]
            bound_plan = remaining.model_copy(
                update={
                    "tasks": tuple(
                        t.model_copy(update={"tool_id": by_id[t.task_id].tool_id, "operation": by_id[t.task_id].operation})
                        for t in remaining.tasks
                    )
                }
            )
        else:
            decisions = []

        return RoutedPlan(
            plan=bound_plan,
            decisions=tuple(decisions),
            unrouted=tuple(unrouted),
            unsupported=proposal.unsupported,
        )

    # ------------------------------------------------------------------ internals

    def _decide(self, task: TaskSpec) -> RoutingDecision | UnroutedTask:
        candidates = self.candidates(task.capability)
        proposed = f"{task.tool_id}.{task.operation}" if task.bound else None
        rejected: str | None = None
        chosen: CapabilityMatch | None = None

        if task.bound:
            rejected = self._check_proposal(task, candidates)
            if rejected is None:
                chosen = next(c for c in candidates if c.tool_id == task.tool_id and c.operation == task.operation)

        if chosen is None:
            if not candidates:
                return UnroutedTask(task_id=task.task_id, capability=task.capability, reason=self._why_none(task.capability))
            chosen = candidates[0]

        if not find_refs(task.inputs):
            try:
                self._registry.validate_input(chosen.tool_id, chosen.operation, task.inputs)
            except InputValidationError as exc:
                exc.details["task_id"] = task.task_id
                raise

        if rejected is None and proposed is not None:
            rationale = "planner-proposed binding verified against the registry"
        else:
            rationale = (
                f"best of {len(candidates)} available candidate(s): "
                f"reliability={chosen.reliability}, cost={chosen.cost}, latency_ms={chosen.expected_latency_ms}"
            )
        decision = RoutingDecision(
            task_id=task.task_id,
            capability=task.capability,
            tool_id=chosen.tool_id,
            operation=chosen.operation,
            rationale=rationale,
            proposed=proposed,
            proposal_rejected=rejected,
            alternatives=tuple(_label(c) for c in candidates if c is not chosen),
        )
        self._audit.record(
            "routing.decision",
            task_id=task.task_id,
            tool_id=chosen.tool_id,
            operation=chosen.operation,
            capability=task.capability,
            rationale=rationale,
            proposal_rejected=rejected,
        )
        return decision

    def _check_proposal(self, task: TaskSpec, candidates: list[CapabilityMatch]) -> str | None:
        """Return why the proposed binding is unacceptable, or None if it is valid."""
        assert task.tool_id is not None and task.operation is not None
        if task.tool_id not in self._registry:
            return f"tool {task.tool_id!r} is not registered"
        op = self._registry.get(task.tool_id).spec.operation(task.operation)
        if op is None:
            return f"tool {task.tool_id!r} has no operation {task.operation!r}"
        if task.capability not in op.capabilities:
            return f"{task.tool_id}.{task.operation} does not provide capability {task.capability!r}"
        if not any(c.tool_id == task.tool_id and c.operation == task.operation for c in candidates):
            return f"{task.tool_id}.{task.operation} is unavailable or not permitted"
        return None

    def _why_none(self, capability: str) -> str:
        all_matches = self._registry.find_by_capability(capability, available_only=False)
        if not all_matches:
            return f"no registered tool provides capability {capability!r}"
        if not any(m.available for m in all_matches):
            names = ", ".join(sorted({_label(m) for m in all_matches}))
            return f"providers of {capability!r} are disabled or unhealthy: {names}"
        return f"no permitted provider of {capability!r}"
