"""Execution engine: runs one bound task with fully resolved arguments.

Order of checks for every task (each failure becomes a terminal ``TaskResult``, never an
exception that escapes the scheduler):

1. tool is registered **and available** (not disabled / unhealthy)
2. arguments match the operation's input schema
3. **final permission check** with the resolved arguments, so the resource is now known —
   provisional plan-time decisions are settled here
4. idempotency: a non-idempotent call carrying an idempotency key runs at most once; a duplicate
   gets the original result (``deduplicated=True``) without re-running or re-approving
5. sensitive operations: an approval must be supplied and is consumed (single use, bound to the
   exact arguments); without one an approval request is created and the task ends
   ``awaiting_approval``
6. non-idempotent calls hold a per-resource lock so writes to the same resource never interleave
7. invoke under the per-tool and global concurrency limits, with the operation's timeout
8. output validated against the output schema

Retry policy: only errors flagged ``recoverable`` are retried, at most
``min(tool.max_retries, max_retries_cap)`` times, with capped exponential backoff and jitter.
Non-idempotent operations are retried only on ``TransientToolError`` (the adapter asserts the
call did not take effect) — never after a timeout, where it may have.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchestrator.config import Settings
from orchestrator.contracts import (
    ErrorInfo,
    ExecutionContext,
    Principal,
    TaskResult,
    TaskSpec,
    TaskStatus,
    ToolSpec,
)
from orchestrator.errors import (
    ApprovalRequiredError,
    OrchestratorError,
    PermissionDeniedError,
    ToolExecutionError,
    ToolTimeoutError,
    TransientToolError,
)
from orchestrator.execution.coordination import IdempotencyStore, ResourceLocks
from orchestrator.observability.audit import AuditLog
from orchestrator.permissions.approvals import ApprovalManager, args_digest
from orchestrator.permissions.manager import PermissionManager
from orchestrator.registry.registry import ToolRegistry


@dataclass(frozen=True)
class RunContext:
    """Per-execution facts shared by all tasks of one plan run."""

    principal: Principal
    request_id: str
    execution_id: str
    idempotency_key: str | None = None
    approvals: Mapping[str, str] = field(default_factory=dict)  # task_id -> approval_id


def _now() -> datetime:
    return datetime.now(UTC)


def status_for(exc: OrchestratorError) -> TaskStatus:
    if isinstance(exc, PermissionDeniedError):
        return TaskStatus.DENIED
    if isinstance(exc, ApprovalRequiredError):
        return TaskStatus.AWAITING_APPROVAL if exc.details.get("status") == "pending" else TaskStatus.DENIED
    if isinstance(exc, ToolTimeoutError) or exc.details.get("reason") == "timeout":
        return TaskStatus.TIMED_OUT
    return TaskStatus.FAILED


class ExecutionEngine:
    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionManager,
        approvals: ApprovalManager,
        audit: AuditLog | None = None,
        *,
        max_concurrency: int = 8,
        max_retries_cap: int = 3,
        retry_base_delay_s: float = 0.2,
        retry_max_delay_s: float = 5.0,
        idempotency: IdempotencyStore | None = None,
        locks: ResourceLocks | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._approvals = approvals
        self._audit = audit or AuditLog()
        self._global = asyncio.Semaphore(max_concurrency)
        self._tool_limits: dict[str, tuple[int, asyncio.Semaphore]] = {}
        self._retries_cap = max_retries_cap
        self._base_delay = retry_base_delay_s
        self._max_delay = retry_max_delay_s
        self._idempotency = idempotency or IdempotencyStore()
        self._locks = locks or ResourceLocks()
        self._sleep = sleep
        self._rng = rng

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        registry: ToolRegistry,
        permissions: PermissionManager,
        approvals: ApprovalManager,
        audit: AuditLog | None = None,
    ) -> "ExecutionEngine":
        return cls(
            registry, permissions, approvals, audit,
            max_concurrency=settings.max_concurrency,
            max_retries_cap=settings.max_retries_cap,
            retry_base_delay_s=settings.retry_base_delay_s,
            retry_max_delay_s=settings.retry_max_delay_s,
            idempotency=IdempotencyStore(settings.idempotency_ttl_s),
        )

    # ------------------------------------------------------------------ public

    async def run_task(self, task: TaskSpec, args: dict[str, Any], run: RunContext) -> TaskResult:
        """Run a bound task whose ``$ref`` inputs have already been resolved into ``args``."""
        if task.tool_id is None or task.operation is None:
            raise ValueError(f"task {task.task_id!r} is not bound to a tool")
        tool_id, op_name = task.tool_id, task.operation
        started = _now()
        self._audit.record("task.started", request_id=run.request_id, execution_id=run.execution_id,
                           task_id=task.task_id, tool_id=tool_id, operation=op_name)

        def finish(status: TaskStatus, **fields: Any) -> TaskResult:
            result = TaskResult(task_id=task.task_id, tool_id=tool_id, operation=op_name, status=status,
                                started_at=started, finished_at=_now(), **fields)
            self._audit.record(
                "task.finished", request_id=run.request_id, execution_id=run.execution_id,
                task_id=task.task_id, tool_id=tool_id, status=status.value, attempts=result.attempts,
                duration_ms=result.duration_ms, deduplicated=result.deduplicated,
                error=result.error.code if result.error else None,
            )
            return result

        # 1–3: availability, input schema, final authorization with the real resource.
        try:
            entry = self._registry.get_available(tool_id)
            op = self._registry.operation(tool_id, op_name)
            self._registry.validate_input(tool_id, op_name, args)
            resource, pending = self._permissions.resource_for(tool_id, op_name, args)
            decision = self._permissions.check(run.principal, tool_id, op_name, args,
                                               request_id=run.request_id, task_id=task.task_id)
            if not decision.allowed or decision.provisional or pending:
                raise PermissionDeniedError(
                    decision.reason if not decision.allowed else "resource could not be determined",
                    details={"tool_id": tool_id, "operation": op_name},
                )
        except OrchestratorError as exc:
            return finish(status_for(exc), error=ErrorInfo.from_exception(exc))

        async def approve_and_execute() -> TaskResult:
            approval_id: str | None = None
            if decision.requires_approval:
                approval_id = run.approvals.get(task.task_id)
                try:
                    if approval_id is None:
                        item = await self._approvals.request(
                            principal=run.principal, request_id=run.request_id, task_id=task.task_id,
                            tool_id=tool_id, operation=op_name, resource=resource, args=args,
                        )
                        err = ApprovalRequiredError(
                            f"{tool_id}.{op_name} requires approval",
                            details={"approval_id": item.approval_id, "status": "pending"},
                        )
                        return finish(TaskStatus.AWAITING_APPROVAL, error=ErrorInfo.from_exception(err),
                                      approval_id=item.approval_id, resource=resource)
                    await self._approvals.consume(approval_id, principal=run.principal, args=args)
                except OrchestratorError as exc:
                    return finish(status_for(exc), error=ErrorInfo.from_exception(exc),
                                  approval_id=approval_id, resource=resource)

            async with self._locks.hold(None if op.idempotent else resource):
                return await self._attempts(entry.adapter, entry.spec, task, args, run, op.idempotent,
                                            resource, approval_id, finish)

        # 4: idempotency for non-idempotent operations.
        if not op.idempotent and run.idempotency_key:
            key = hashlib.sha256(json.dumps(
                [run.principal.user_id, run.idempotency_key, tool_id, op_name, args_digest(args)]
            ).encode()).hexdigest()
            result = await self._idempotency.run_once(key, approve_and_execute)
            if result.deduplicated:
                self._audit.record("task.deduplicated", request_id=run.request_id, execution_id=run.execution_id,
                                   task_id=task.task_id, tool_id=tool_id)
                # Report it under this task's id; the payload is the original call's.
                result = result.model_copy(update={"task_id": task.task_id})
            return result
        return await approve_and_execute()

    # ------------------------------------------------------------------ internals

    async def _attempts(self, adapter, spec: ToolSpec, task: TaskSpec, args: dict[str, Any], run: RunContext,
                        idempotent: bool, resource: str | None, approval_id: str | None, finish) -> TaskResult:
        assert task.operation is not None
        timeout = spec.timeout_for(task.operation)
        max_retries = min(spec.constraints.max_retries, self._retries_cap)
        attempt = 0
        while True:
            attempt += 1
            ctx = ExecutionContext(
                request_id=run.request_id, execution_id=run.execution_id, task_id=task.task_id,
                principal=run.principal, attempt=attempt, idempotency_key=run.idempotency_key, resource=resource,
            )
            try:
                async with self._tool_semaphore(spec), self._global:  # tool slot first: never idle a global slot
                    output = await asyncio.wait_for(adapter.invoke(task.operation, args, ctx), timeout)
                self._registry.validate_output(spec.tool_id, task.operation, output)
                return finish(TaskStatus.SUCCEEDED, output=output, attempts=attempt,
                              approval_id=approval_id, resource=resource)
            except TimeoutError:
                err: OrchestratorError = ToolTimeoutError(
                    f"{spec.tool_id}.{task.operation} exceeded {timeout}s", details={"timeout_s": timeout}
                )
            except OrchestratorError as exc:
                err = exc
            except Exception as exc:  # adapter bug: never retried
                err = ToolExecutionError(f"unexpected {type(exc).__name__}: {exc}"[:500])

            retryable = (
                err.recoverable
                and attempt <= max_retries
                and (idempotent or (isinstance(err, TransientToolError) and not isinstance(err, ToolTimeoutError)))
            )
            if not retryable:
                return finish(status_for(err), error=ErrorInfo.from_exception(err), attempts=attempt,
                              approval_id=approval_id, resource=resource)
            delay = min(self._max_delay, self._base_delay * (2 ** (attempt - 1))) * (0.5 + self._rng() / 2)
            self._audit.record("task.retry", request_id=run.request_id, execution_id=run.execution_id,
                               task_id=task.task_id, tool_id=spec.tool_id, attempt=attempt,
                               error=err.code, delay_s=round(delay, 3))
            await self._sleep(delay)

    def _tool_semaphore(self, spec: ToolSpec) -> asyncio.Semaphore:
        limit = spec.constraints.max_concurrency
        current = self._tool_limits.get(spec.tool_id)
        if current is None or current[0] != limit:
            current = (limit, asyncio.Semaphore(limit))
            self._tool_limits[spec.tool_id] = current
        return current[1]
