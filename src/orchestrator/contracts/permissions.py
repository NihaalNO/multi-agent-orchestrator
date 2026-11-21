"""Permission contracts.

The ``Principal`` comes from authentication only. Nothing in a plan or LLM output can add
roles, raise an access level, or mark an operation as non-sensitive — the policy reads those
from the registered ``OperationSpec``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts.tools import AccessLevel, OperationSpec, ToolSpec


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1)
    roles: frozenset[str] = frozenset()


class PermissionGrant(BaseModel):
    """Allows ``subject`` (a role, or ``user:<id>``) to use matching operations.

    ``tool_id`` / ``operation`` accept ``*``. ``resources`` are fnmatch-style patterns; an
    empty tuple means "no resource restriction".
    """

    model_config = ConfigDict(frozen=True)

    subject: str
    tool_id: str = "*"
    operation: str = "*"
    max_access: AccessLevel = AccessLevel.READ
    resources: tuple[str, ...] = ()
    allow_sensitive: bool = Field(
        default=False,
        description="Even if True, sensitive operations still require approval; this only "
        "makes them approvable rather than outright denied.",
    )


class PermissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    requires_approval: bool = False
    reason: str
    matched_grant: PermissionGrant | None = None


@runtime_checkable
class PermissionPolicy(Protocol):
    """Deterministic authorization. Implemented in Phase 04."""

    def check(
        self,
        principal: Principal,
        tool: ToolSpec,
        operation: OperationSpec,
        resource: str | None = None,
    ) -> PermissionDecision: ...
