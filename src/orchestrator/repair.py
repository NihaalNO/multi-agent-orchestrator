"""Re-planning after plan-caused failures.

A planner writes its plan before any data has been read, so it can guess wrong: a column name, the
shape of a tool's output, the way inputs reach a snippet of code. When a run fails *because of the
plan*, the orchestrator can show the planner what happened and let it produce a corrected plan.

Deliberately narrow:

* **Which failures qualify** (``repairable_failures``): only non-recoverable errors the plan caused —
  ``tool_execution_error`` (e.g. code raised, file not found), ``input_validation_error`` and
  ``input_resolution_error`` (a ``$ref`` to a field that does not exist). Never permission denials,
  pending approvals, unavailable tools, timeouts, cancellations or transient errors.
* **Only side-effect-free steps**: every failed task must be a ``read`` or ``execute`` operation that
  is not sensitive. A failed write is never re-planned, so a corrected plan cannot repeat a side
  effect.
* **What the planner is shown** (``build_feedback``): each task's status, the error message (and the
  tail of a Python traceback), the failed tasks' own inputs as planned, and the *shape* of
  successful outputs — key names, column names, list lengths, value types. Data values are not
  included, though an error message can quote a small fragment of the value that caused it.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from orchestrator.contracts import AccessLevel, ExecutionPlan, ExecutionReport, TaskResult, TaskSpec, TaskStatus
from orchestrator.registry.registry import ToolRegistry

REPAIRABLE_CODES = frozenset({"tool_execution_error", "input_validation_error", "input_resolution_error"})
_BLOCKING = frozenset({TaskStatus.DENIED, TaskStatus.AWAITING_APPROVAL, TaskStatus.CANCELLED})
_SAFE_ACCESS = frozenset({AccessLevel.READ, AccessLevel.EXECUTE})
_MAX_TEXT = 700


class AttemptSummary(BaseModel):
    """One failed attempt that led to a re-plan (kept on the request record)."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    failed: dict[str, str]  # task_id -> "error_code: message"


def repairable_failures(plan: ExecutionPlan, report: ExecutionReport, registry: ToolRegistry) -> list[TaskResult]:
    """The failed tasks a re-plan may fix, or [] if this run must not be re-planned."""
    statuses = {r.status for r in report.task_results}
    if statuses & _BLOCKING:
        return []  # a permission or approval question is for a human, not for the planner
    failed = [r for r in report.task_results if r.status == TaskStatus.FAILED]
    if not failed:
        return []
    for r in failed:
        if r.error is None or r.error.recoverable or r.error.code not in REPAIRABLE_CODES:
            return []
        task = plan.task(r.task_id)
        if task.tool_id is None or task.operation is None or task.tool_id not in registry:
            return []
        op = registry.get(task.tool_id).spec.operation(task.operation)
        if op is None or op.sensitive or op.access not in _SAFE_ACCESS:
            return []
    return failed


def shape(value: Any, depth: int = 0) -> str:
    """Structure without data, as plain text:
    ``{rows: list[6] of {month: string, Revenue: string}, row_count: number}``.
    Plain text rather than JSON, so it is not escaped again inside the planner's prompt."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        keys = list(value)[:25]
        parts = [f"{k}: {shape(value[k], depth + 1)}" for k in keys]
        if len(value) > len(keys):
            parts.append(f"... {len(value) - len(keys)} more keys")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        return f"list[{len(value)}] of {shape(value[0], depth + 1)}" if value else "list[0]"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "null" if value is None else type(value).__name__


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def build_feedback(plan: ExecutionPlan, report: ExecutionReport, failed: list[TaskResult]) -> str:
    results = {r.task_id: r for r in report.task_results}
    failed_ids = {r.task_id for r in failed}
    lines = ["Tasks and outcomes of the previous plan:"]
    for task in plan.tasks:
        r = results.get(task.task_id)
        status = r.status.value if r else "not run"
        lines.append(f"- {task.task_id} [{task.capability} via {task.tool_id}.{task.operation}]: {status}")
        if r is None:
            continue
        if r.status == TaskStatus.SUCCEEDED and r.output is not None:
            lines.append(f"  output shape: {shape(r.output)}")
        if task.task_id in failed_ids and r.error is not None:
            lines.append(f"  error: {r.error.code}: {_clip(r.error.message, 300)}")
            tb = r.error.details.get("traceback") if r.error.details else None
            if tb:
                lines.append(f"  traceback (end): {_clip(str(tb))}")
            lines.append(f"  inputs as planned: {_clip(json.dumps(task.inputs), 1200)}")
    return "\n".join(lines)


def same_call(a: TaskSpec, b: TaskSpec) -> bool:
    """Would ``b`` do exactly what ``a`` did? Then ``a``'s successful result can stand in for ``b``."""
    return (a.tool_id, a.operation, a.inputs, a.depends_on) == (b.tool_id, b.operation, b.inputs, b.depends_on)


def reusable_results(old_plan: ExecutionPlan, report: ExecutionReport, new_plan: ExecutionPlan) -> dict[str, TaskResult]:
    """Successful results from the old run that the new plan repeats unchanged, keyed by the new
    plan's task ids. Dependencies are compared by id, so a task is only reused if its upstream tasks
    were also kept under the same ids."""
    old_ok = {r.task_id: r for r in report.task_results if r.status == TaskStatus.SUCCEEDED}
    reuse: dict[str, TaskResult] = {}
    for new in new_plan.tasks:
        for old in old_plan.tasks:
            if old.task_id in old_ok and same_call(old, new):
                reuse[new.task_id] = old_ok[old.task_id].model_copy(update={"task_id": new.task_id})
                break
    return reuse
