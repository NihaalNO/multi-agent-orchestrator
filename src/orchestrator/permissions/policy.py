"""Default-deny, grant-based permission policy (RBAC + operation + resource scoping).

Evaluation for (principal, tool, operation, resource):

1. Collect grants whose subject is one of the principal's subjects (``user:<id>``,
   ``role:<r>``) and whose ``tool_id`` / ``operation`` match (exact or ``*``).
2. Any matching **deny** grant wins → denied.
3. Allow grants must also cover the operation's *registered* access level and the resource.
4. **Sensitive** operations additionally need ``allow_sensitive`` on the grant, and are then
   allowed only *with human approval* (``requires_approval=True``).
5. Nothing matched → denied (default deny).

Resource matching is case-insensitive (``fnmatch`` over case-folded strings) so a deny rule
for ``file:secret/*`` cannot be bypassed with ``SECRET/x`` on a case-insensitive filesystem.
When the resource is still pending (it depends on an upstream output), resource-scoped rules
match *provisionally*; the decision is marked ``provisional`` and must be re-checked with the
real resource before execution.

This module is pure and synchronous: no I/O, no LLM, no plan-supplied inputs.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Iterable, Literal

from orchestrator.contracts import (
    OperationSpec,
    PermissionDecision,
    PermissionGrant,
    Principal,
    ToolSpec,
)

_Match = Literal["yes", "no", "provisional"]


def _name_matches(pattern: str, value: str) -> bool:
    return pattern == "*" or pattern == value


def _resource_match(grant: PermissionGrant, resource: str | None, pending: bool) -> _Match:
    if not grant.resources:
        return "yes"
    if pending:
        return "provisional"
    if resource is None:
        return "no"
    folded = resource.casefold()
    return "yes" if any(fnmatchcase(folded, p.casefold()) for p in grant.resources) else "no"


class GrantPolicy:
    def __init__(self, grants: Iterable[PermissionGrant] = ()) -> None:
        self._grants: tuple[PermissionGrant, ...] = tuple(grants)

    @property
    def grants(self) -> tuple[PermissionGrant, ...]:
        return self._grants

    def check(
        self,
        principal: Principal,
        tool: ToolSpec,
        operation: OperationSpec,
        resource: str | None = None,
        *,
        resource_pending: bool = False,
    ) -> PermissionDecision:
        subjects = principal.subjects
        applicable = [
            g
            for g in self._grants
            if g.subject in subjects
            and _name_matches(g.tool_id, tool.tool_id)
            and _name_matches(g.operation, operation.name)
        ]
        target = f"{tool.tool_id}.{operation.name}" + (f" on {resource}" if resource else "")

        # 1. Deny rules override everything they (possibly) match.
        provisional = False
        for g in applicable:
            if g.effect != "deny":
                continue
            m = _resource_match(g, resource, resource_pending)
            if m == "yes":
                return PermissionDecision(allowed=False, reason=f"denied by rule for {g.subject}: {target}", matched_grant=g)
            if m == "provisional":
                provisional = True  # may still be denied once the resource is known

        # 2. Allow rules: access level + resource.
        allows: list[tuple[PermissionGrant, _Match]] = []
        insufficient_access = False
        for g in applicable:
            if g.effect != "allow":
                continue
            if not g.max_access.covers(operation.access):
                insufficient_access = True
                continue
            m = _resource_match(g, resource, resource_pending)
            if m != "no":
                allows.append((g, m))

        if not allows:
            if insufficient_access:
                reason = f"{operation.access.value} access required for {target}; grants only allow less"
            elif applicable:
                reason = f"no grant covers the resource for {target}"
            else:
                reason = f"no grant allows {target} (default deny)"
            return PermissionDecision(allowed=False, reason=reason)

        # 3. Sensitive operations: need allow_sensitive, then human approval.
        if operation.sensitive:
            allows = [(g, m) for g, m in allows if g.allow_sensitive]
            if not allows:
                return PermissionDecision(
                    allowed=False,
                    reason=f"{target} is sensitive and no grant permits requesting approval for it",
                )

        # Prefer a definite match over a provisional one.
        allows.sort(key=lambda gm: gm[1] != "yes")
        grant, m = allows[0]
        return PermissionDecision(
            allowed=True,
            requires_approval=operation.sensitive,
            provisional=provisional or m == "provisional",
            reason=(
                f"allowed by grant for {grant.subject}"
                + (" (requires approval)" if operation.sensitive else "")
                + (" (provisional: resource not yet known)" if provisional or m == "provisional" else "")
            ),
            matched_grant=grant,
        )
