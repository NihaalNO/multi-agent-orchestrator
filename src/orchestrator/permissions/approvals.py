"""Human approval workflow for sensitive operations.

An approval is bound to *exactly one* call: the requesting user, request, task, tool,
operation, resource and a SHA-256 digest of the canonical arguments. It is:

* decided only by a principal holding the approver role, never by the requester themself;
* time-limited (``ttl``) and single-use (``consume`` marks it ``consumed``);
* invalid for any other arguments — approving "delete row 5" cannot authorize "delete all".

Storage is in-memory (spec §4: add persistence only when required). Every transition is
audited.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from orchestrator.contracts import ApprovalRequest, ApprovalStatus, Principal
from orchestrator.errors import ApprovalRequiredError, OrchestratorError, PermissionDeniedError
from orchestrator.observability.audit import AuditLog, redact

_PREVIEW_CHARS = 500


class ApprovalNotFoundError(OrchestratorError):
    code = "approval_not_found"


def args_digest(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _preview(args: dict[str, Any]) -> str:
    text = json.dumps(redact(args), sort_keys=True, ensure_ascii=False)
    return text if len(text) <= _PREVIEW_CHARS else text[: _PREVIEW_CHARS - 1] + "…"


class ApprovalManager:
    def __init__(
        self,
        audit: AuditLog | None = None,
        *,
        approver_role: str = "approver",
        ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._audit = audit or AuditLog()
        self._approver_role = approver_role
        self._ttl = ttl
        self._now = clock or (lambda: datetime.now(UTC))
        self._items: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    @property
    def approver_role(self) -> str:
        return self._approver_role

    # ------------------------------------------------------------------ requester side

    async def request(
        self,
        *,
        principal: Principal,
        request_id: str,
        task_id: str,
        tool_id: str,
        operation: str,
        resource: str | None,
        args: dict[str, Any],
    ) -> ApprovalRequest:
        """Create a pending approval, or return the live one for the identical call
        (so re-submitting the same request does not spam approvers)."""
        digest = args_digest(args)
        async with self._lock:
            self._expire_locked()
            for item in self._items.values():
                if (
                    item.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
                    and (item.requested_by, item.request_id, item.task_id, item.tool_id, item.operation, item.args_digest)
                    == (principal.user_id, request_id, task_id, tool_id, operation, digest)
                ):
                    return item
            now = self._now()
            item = ApprovalRequest(
                approval_id=uuid.uuid4().hex,
                request_id=request_id,
                task_id=task_id,
                requested_by=principal.user_id,
                tool_id=tool_id,
                operation=operation,
                resource=resource,
                args_digest=digest,
                args_preview=_preview(args),
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._items[item.approval_id] = item
        self._audit.record(
            "approval.requested",
            request_id=request_id,
            task_id=task_id,
            tool_id=tool_id,
            approval_id=item.approval_id,
            operation=operation,
            requested_by=principal.user_id,
        )
        return item

    async def consume(self, approval_id: str, *, principal: Principal, args: dict[str, Any]) -> ApprovalRequest:
        """Use an approval for execution. Succeeds once, only for the same user and the
        byte-identical (canonical) arguments that were approved."""
        async with self._lock:
            self._expire_locked()
            item = self._require(approval_id)
            if item.requested_by != principal.user_id:
                raise PermissionDeniedError("approval belongs to a different user", details={"approval_id": approval_id})
            if item.status != ApprovalStatus.APPROVED:
                raise ApprovalRequiredError(
                    f"approval is {item.status.value}", details={"approval_id": approval_id, "status": item.status.value}
                )
            if args_digest(args) != item.args_digest:
                raise PermissionDeniedError(
                    "arguments differ from the approved arguments", details={"approval_id": approval_id}
                )
            item = item.model_copy(update={"status": ApprovalStatus.CONSUMED})
            self._items[approval_id] = item
        self._audit.record("approval.consumed", request_id=item.request_id, task_id=item.task_id,
                           tool_id=item.tool_id, approval_id=approval_id)
        return item

    # ------------------------------------------------------------------ approver side

    async def decide(
        self, approval_id: str, *, approver: Principal, approve: bool, note: str | None = None
    ) -> ApprovalRequest:
        if self._approver_role not in approver.roles:
            raise PermissionDeniedError(f"approving requires the {self._approver_role!r} role")
        async with self._lock:
            self._expire_locked()
            item = self._require(approval_id)
            if item.requested_by == approver.user_id:
                raise PermissionDeniedError("users cannot approve their own requests")
            if item.status != ApprovalStatus.PENDING:
                raise ApprovalRequiredError(
                    f"approval is already {item.status.value}", details={"approval_id": approval_id}
                )
            item = item.model_copy(
                update={
                    "status": ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED,
                    "decided_by": approver.user_id,
                    "decided_at": self._now(),
                    "note": (note or None) and note[:1000],
                }
            )
            self._items[approval_id] = item
        self._audit.record(
            "approval.approved" if approve else "approval.denied",
            request_id=item.request_id, task_id=item.task_id, tool_id=item.tool_id,
            approval_id=approval_id, decided_by=approver.user_id,
        )
        return item

    # ------------------------------------------------------------------ queries

    def get(self, approval_id: str) -> ApprovalRequest:
        item = self._require(approval_id)
        if item.status == ApprovalStatus.PENDING and self._now() >= item.expires_at:
            return item.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return item

    def list(self, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        items = [self.get(i) for i in self._items]
        return sorted(
            (i for i in items if status is None or i.status == status), key=lambda i: i.created_at
        )

    # ------------------------------------------------------------------ internals

    def _require(self, approval_id: str) -> ApprovalRequest:
        try:
            return self._items[approval_id]
        except KeyError:
            raise ApprovalNotFoundError(f"approval {approval_id!r} not found") from None

    def _expire_locked(self) -> None:
        now = self._now()
        for aid, item in list(self._items.items()):
            if item.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED) and now >= item.expires_at:
                self._items[aid] = item.model_copy(update={"status": ApprovalStatus.EXPIRED})
                self._audit.record("approval.expired", request_id=item.request_id, task_id=item.task_id,
                                   tool_id=item.tool_id, approval_id=aid)
