"""DAG scheduler: runs a bound plan with dependency-aware concurrency.

* **Ready-queue, not layer-by-layer**: a task starts as soon as *its own* dependencies have
  succeeded, so a slow task in one branch never holds up an unrelated branch.
* ``$ref`` inputs are resolved from upstream outputs just before a task starts.
* **Partial failure**: when a task does not succeed, every task that (transitively) depends on
  it is marked ``skipped``; independent branches keep running.
* **Deadline**: an optional whole-plan time limit cancels running tasks (``cancelled``) and
  marks unstarted ones ``cancelled`` too.
* **Cancellation**: if the caller cancels ``execute``, running tasks are cancelled and awaited
  (so tools can clean up) before the cancellation propagates.

Concurrency limits (global and per tool) are enforced by the engine, which is shared across
executions, so they hold across concurrent requests too.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

from orchestrator.contracts import (
    ErrorInfo,
    ExecutionPlan,
    ExecutionReport,
    ExecutionStatus,
    Principal,
    TaskResult,
    TaskStatus,
)
from orchestrator.errors import ExecutionCancelledError, InputResolutionError
from orchestrator.execution.engine import ExecutionEngine, RunContext
from orchestrator.execution.refs import resolve
from orchestrator.observability.audit import AuditLog

_FAILURES = {TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.DENIED, TaskStatus.CANCELLED}


def overall_status(results: list[TaskResult]) -> ExecutionStatus:
    statuses = {r.status for r in results}
    succeeded = TaskStatus.SUCCEEDED in statuses
    if statuses & _FAILURES:
        return ExecutionStatus.PARTIAL if succeeded else ExecutionStatus.FAILED
    if TaskStatus.AWAITING_APPROVAL in statuses:
        return ExecutionStatus.AWAITING_APPROVAL
    return ExecutionStatus.SUCCEEDED if statuses == {TaskStatus.SUCCEEDED} else ExecutionStatus.PARTIAL


class DagScheduler:
    def __init__(self, engine: ExecutionEngine, audit: AuditLog | None = None) -> None:
        self._engine = engine
        self._audit = audit or AuditLog()

    async def execute(
        self,
        plan: ExecutionPlan,
        *,
        principal: Principal,
        execution_id: str | None = None,
        idempotency_key: str | None = None,
        approvals: Mapping[str, str] | None = None,
        deadline_s: float | None = None,
    ) -> ExecutionReport:
        unbound = [t.task_id for t in plan.tasks if t.tool_id is None]
        if unbound:
            raise ValueError(f"plan has unbound tasks (route it first): {unbound}")
        run = RunContext(
            principal=principal,
            request_id=plan.request_id,
            execution_id=execution_id or uuid.uuid4().hex,
            idempotency_key=idempotency_key,
            approvals=dict(approvals or {}),
        )
        self._audit.record("execution.started", request_id=run.request_id, execution_id=run.execution_id,
                           plan_id=plan.plan_id, tasks=[t.task_id for t in plan.tasks], layers=plan.layers())

        results: dict[str, TaskResult] = {}
        outputs: dict[str, Any] = {}
        waiting: dict[str, set[str]] = {t.task_id: set(t.depends_on) for t in plan.tasks}
        running: dict[asyncio.Task[TaskResult], str] = {}

        def terminal(task_id: str, status: TaskStatus, code: str, message: str) -> None:
            t = plan.task(task_id)
            results[task_id] = TaskResult(
                task_id=task_id, tool_id=t.tool_id, operation=t.operation, status=status,
                error=ErrorInfo(code=code, message=message), finished_at=datetime.now(UTC),
            )

        def settle(task_id: str, result: TaskResult) -> None:
            results[task_id] = result
            if result.status == TaskStatus.SUCCEEDED:
                outputs[task_id] = result.output
                for deps in waiting.values():
                    deps.discard(task_id)
                return
            for dep in sorted(plan.dependents(task_id)):
                if dep not in results:
                    waiting.pop(dep, None)
                    terminal(dep, TaskStatus.SKIPPED, "upstream_not_succeeded",
                             f"upstream task {task_id!r} ended {result.status.value}")

        def start_ready() -> None:
            for task_id in sorted(tid for tid, deps in waiting.items() if not deps):
                del waiting[task_id]
                task = plan.task(task_id)
                try:
                    args = resolve(task.inputs, outputs)
                except InputResolutionError as exc:
                    settle(task_id, TaskResult(task_id=task_id, tool_id=task.tool_id, operation=task.operation,
                                               status=TaskStatus.FAILED, error=ErrorInfo.from_exception(exc),
                                               finished_at=datetime.now(UTC)))
                    continue
                running[asyncio.create_task(self._engine.run_task(task, args, run), name=f"task:{task_id}")] = task_id

        async def cancel_running(code: str, message: str) -> None:
            for t in running:
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            for t, task_id in running.items():
                if not t.cancelled() and t.exception() is None:
                    settle(task_id, t.result())  # finished just before cancellation landed
                else:
                    terminal(task_id, TaskStatus.CANCELLED, code, message)
            running.clear()
            for task_id in list(waiting):
                terminal(task_id, TaskStatus.CANCELLED, code, message)
            waiting.clear()

        try:
            async with asyncio.timeout(deadline_s):
                start_ready()
                while running:
                    done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for t in sorted(done, key=lambda t: running[t]):
                        task_id = running.pop(t)
                        exc = t.exception()
                        if exc is None:
                            settle(task_id, t.result())
                        else:  # engine bug: contain it to this task
                            settle(task_id, TaskResult(
                                task_id=task_id, tool_id=plan.task(task_id).tool_id,
                                operation=plan.task(task_id).operation, status=TaskStatus.FAILED,
                                error=ErrorInfo.from_exception(exc), finished_at=datetime.now(UTC),
                            ))
                    start_ready()
        except TimeoutError:
            await cancel_running("execution_deadline", f"execution exceeded its {deadline_s}s deadline")
        except asyncio.CancelledError:
            await cancel_running(ExecutionCancelledError.code, "execution was cancelled")
            raise

        for task_id in list(waiting):  # defensive: nothing should remain
            terminal(task_id, TaskStatus.SKIPPED, "never_ready", "dependencies were never satisfied")

        ordered = [results[t.task_id] for t in plan.tasks]
        report = ExecutionReport(
            request_id=run.request_id,
            execution_id=run.execution_id,
            plan_id=plan.plan_id,
            status=overall_status(ordered),
            task_results=tuple(ordered),
            final_output={k: outputs[k] for k in (t.task_id for t in plan.tasks) if k in outputs},
        )
        self._audit.record("execution.finished", request_id=run.request_id, execution_id=run.execution_id,
                           status=report.status.value,
                           tasks={r.task_id: r.status.value for r in ordered})
        return report
