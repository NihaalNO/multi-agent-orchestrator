"""Permission manager: applies the policy to concrete tool calls.

It resolves the operation's registered metadata from the registry and asks the tool adapter
for the resource the call touches, so both the access level and the resource come from
trusted code, not from the plan. Every decision on a planned or executed call is audited.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from orchestrator.contracts import (
    CapabilityMatch,
    OperationSpec,
    PermissionDecision,
    PermissionPolicy,
    Principal,
    RoutedPlan,
    TaskSpec,
    ToolSpec,
)
from orchestrator.errors import PermissionDeniedError
from orchestrator.observability.audit import AuditLog
from orchestrator.registry.registry import ToolRegistry
from orchestrator.tools.base import RESOURCE_PENDING


class PlanAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    decisions: dict[str, PermissionDecision]

    @property
    def denied(self) -> list[str]:
        return sorted(t for t, d in self.decisions.items() if not d.allowed)

    @property
    def approval_required(self) -> list[str]:
        return sorted(t for t, d in self.decisions.items() if d.allowed and d.requires_approval)

    @property
    def provisional(self) -> list[str]:
        return sorted(t for t, d in self.decisions.items() if d.allowed and d.provisional)

    @property
    def allowed(self) -> bool:
        return not self.denied


class PermissionManager:
    def __init__(self, policy: PermissionPolicy, registry: ToolRegistry, audit: AuditLog | None = None) -> None:
        self._policy = policy
        self._registry = registry
        self._audit = audit or AuditLog()

    def resource_for(self, tool_id: str, operation: str, args: dict[str, Any]) -> tuple[str | None, bool]:
        """(resource, pending) for a call, as reported by the tool adapter."""
        res = self._registry.get(tool_id).adapter.resource_for(operation, args)
        if res is RESOURCE_PENDING:
            return None, True
        if res is not None and not isinstance(res, str):
            raise TypeError(f"{tool_id}.resource_for returned {type(res).__name__}")
        return res, False

    def check(
        self,
        principal: Principal,
        tool_id: str,
        operation: str,
        args: dict[str, Any],
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        audit: bool = True,
    ) -> PermissionDecision:
        spec = self._registry.get(tool_id).spec
        op = self._registry.operation(tool_id, operation)
        resource, pending = self.resource_for(tool_id, operation, args)
        decision = self._policy.check(principal, spec, op, resource, resource_pending=pending)
        if audit:
            self._audit.record(
                "permission.decision",
                request_id=request_id,
                task_id=task_id,
                tool_id=tool_id,
                user_id=principal.user_id,
                operation=operation,
                resource=resource,
                resource_pending=pending,
                allowed=decision.allowed,
                requires_approval=decision.requires_approval,
                provisional=decision.provisional,
                reason=decision.reason,
            )
        return decision

    def require(
        self,
        principal: Principal,
        tool_id: str,
        operation: str,
        args: dict[str, Any],
        *,
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> PermissionDecision:
        """Like ``check`` but raises ``PermissionDeniedError`` when denied. Intended for the
        execution engine's final check with fully resolved arguments."""
        decision = self.check(principal, tool_id, operation, args, request_id=request_id, task_id=task_id)
        if not decision.allowed:
            raise PermissionDeniedError(
                decision.reason,
                details={"tool_id": tool_id, "operation": operation, "task_id": task_id},
            )
        return decision

    def operation_visible(self, principal: Principal, spec: ToolSpec, op: OperationSpec) -> bool:
        """Could this principal use the operation on *some* resource? Used to hide tools from
        the planner's catalog that the principal can never use."""
        return self._policy.check(principal, spec, op, None, resource_pending=True).allowed

    def candidate_filter(self, principal: Principal) -> Callable[[CapabilityMatch, TaskSpec], bool]:
        """Router hook: is this candidate permitted for this task's actual inputs?"""

        def allowed(match: CapabilityMatch, task: TaskSpec) -> bool:
            return self.check(principal, match.tool_id, match.operation, task.inputs, audit=False).allowed

        return allowed

    def authorize_plan(self, principal: Principal, routed: RoutedPlan, *, request_id: str | None = None) -> PlanAuthorization:
        decisions: dict[str, PermissionDecision] = {}
        if routed.plan is not None:
            for task in routed.plan.tasks:
                assert task.tool_id is not None and task.operation is not None
                decisions[task.task_id] = self.check(
                    principal, task.tool_id, task.operation, task.inputs,
                    request_id=request_id, task_id=task.task_id,
                )
        return PlanAuthorization(decisions=decisions)
