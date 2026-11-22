"""Permission contracts.

The ``Principal`` comes from authentication only. Nothing in a plan or LLM output can add
roles, raise an access level, mark an operation as non-sensitive, or choose the resource being
checked — the policy reads access/sensitivity from the registered ``OperationSpec`` and the
resource is derived by the tool adapter from the operation's actual arguments.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts.tools import AccessLevel, OperationSpec, ToolSpec

SUBJECT_PATTERN = r"^(role|user):[A-Za-z0-9_.@-]+$"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.@-]+$")
    roles: frozenset[str] = frozenset()

    @property
    def subjects(self) -> frozenset[str]:
        return frozenset({f"user:{self.user_id}", *(f"role:{r}" for r in self.roles)})


class PermissionGrant(BaseModel):
    """One policy rule.

    * ``subject``: ``role:<name>`` or ``user:<id>``.
    * ``tool_id`` / ``operation``: exact name or ``*``.
    * ``resources``: glob patterns (``fnmatch``, case-insensitive) over adapter-derived resource
      strings such as ``file:reports/q1.csv``. Empty means "any resource".
    * ``effect="deny"`` rules override every allow rule they match.
    * ``allow_sensitive``: sensitive operations still need human approval; without this flag
      they are denied outright.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(pattern=SUBJECT_PATTERN)
    tool_id: str = "*"
    operation: str = "*"
    effect: Literal["allow", "deny"] = "allow"
    max_access: AccessLevel = AccessLevel.READ
    resources: tuple[str, ...] = ()
    allow_sensitive: bool = False
    description: str = ""


class PermissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    requires_approval: bool = False
    provisional: bool = Field(
        default=False,
        description="The resource was not yet known (depends on an upstream output); the "
        "decision must be re-checked with the real resource before execution.",
    )
    reason: str
    matched_grant: PermissionGrant | None = None


@runtime_checkable
class PermissionPolicy(Protocol):
    """Deterministic authorization. Must not consult an LLM or any plan-supplied data."""

    def check(
        self,
        principal: Principal,
        tool: ToolSpec,
        operation: OperationSpec,
        resource: str | None = None,
        *,
        resource_pending: bool = False,
    ) -> PermissionDecision: ...


# --- Approvals --------------------------------------------------------------------------------


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ApprovalRequest(BaseModel):
    """A request for a human to approve one sensitive operation with exact arguments."""

    model_config = ConfigDict(frozen=True)

    approval_id: str
    request_id: str
    task_id: str
    requested_by: str
    tool_id: str
    operation: str
    resource: str | None
    args_digest: str = Field(description="SHA-256 of the canonical JSON arguments.")
    args_preview: str = Field(description="Redacted, truncated arguments shown to the approver.")
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str | None = None
