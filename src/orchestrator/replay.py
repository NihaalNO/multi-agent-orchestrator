"""Replay: run a finished request's plan again and compare every step with the original.

"Would this still give the same answer today?" A replay re-executes the **stored plan** (no planner,
so differences come from tools and data, never the model) under the requester's **current**
permissions, with a fresh idempotency key so every step really runs instead of returning a cached
result. Then it compares each step's output with the original, ignoring fields that change on every
run anyway (retrieval time, timings, printed output).

Safety: only steps that read or compute (``read`` / ``execute``, not sensitive) are re-run. A step
that writes or needs approval is never repeated, and neither is anything that depends on one; both
are reported as not re-run, with the reason. The original record is not changed, and the written
answer is not re-written: a replay is about the tool steps it rests on.

The report also says whether the receipt still matches the record and whether the permission policy
changed since the original run, the two things besides data that explain a different outcome.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts import AccessLevel, TaskStatus
from orchestrator.receipts import digest, policy_digest, verify_against_record
from orchestrator.resolution.values import DEFAULT_IGNORE_KEYS, flatten

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator.core import Orchestrator, RequestRecord
    from orchestrator.contracts import Principal

_SAFE_ACCESS = frozenset({AccessLevel.READ, AccessLevel.EXECUTE})
_MAX_CHANGED = 8

Outcome = Literal["identical", "differs", "failed", "not_rerun"]


class StepReplay(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    tool_id: str | None
    operation: str | None
    outcome: Outcome
    original_status: str | None = None
    replay_status: str | None = None
    original_sha256: str | None = None
    replay_sha256: str | None = None
    changed: tuple[str, ...] = Field(default=(), description="Paths whose values differ (first few).")
    original_ms: float | None = None
    replay_ms: float | None = None
    note: str = ""


class ReplayReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    replay_id: str
    replayed_at: str
    verdict: Literal["identical", "differs", "incomplete"]
    steps: tuple[StepReplay, ...]
    counts: dict[str, int]
    receipt_matches_record: bool | None = Field(default=None, description="None when the request has no receipt.")
    policy_changed: bool | None = Field(default=None, description="None when the request has no receipt.")
    note: str = "The written answer is not re-written; only the tool steps it rests on are re-run."


def _changed_paths(before: Any, after: Any) -> tuple[str, ...]:
    a, b = flatten(before, DEFAULT_IGNORE_KEYS), flatten(after, DEFAULT_IGNORE_KEYS)
    paths = sorted(k for k in a.keys() | b.keys() if a.get(k, _MISSING) != b.get(k, _MISSING))
    return tuple(paths[:_MAX_CHANGED]) + (("…",) if len(paths) > _MAX_CHANGED else ())


_MISSING = object()


def safe_to_rerun(orchestrator: "Orchestrator", tool_id: str | None, operation: str | None) -> tuple[bool, str]:
    registry = orchestrator.registry
    if tool_id is None or operation is None or tool_id not in registry:
        return False, "its tool is no longer registered"
    op = registry.get(tool_id).spec.operation(operation)
    if op is None:
        return False, "its operation no longer exists"
    if op.sensitive or op.access not in _SAFE_ACCESS:
        return False, "it has side effects (writes or needs approval), so it is never repeated"
    return True, ""


async def replay_request(orchestrator: "Orchestrator", record: "RequestRecord", principal: "Principal") -> ReplayReport:
    assert record.plan is not None and record.report is not None
    plan = record.plan
    original = {r.task_id: r for r in record.report.task_results}

    unsafe: dict[str, str] = {}
    for task in plan.tasks:
        ok, why = safe_to_rerun(orchestrator, task.tool_id, task.operation)
        if not ok:
            unsafe[task.task_id] = why
    dropped = set(unsafe)
    for tid in list(unsafe):
        for dep in plan.dependents(tid):
            if dep not in unsafe:
                unsafe[dep] = f"it depends on {tid}, which is not re-run"
        dropped |= plan.dependents(tid)
    runnable = plan.without(dropped) if dropped else plan

    replayed: dict[str, Any] = {}
    replay_id = uuid.uuid4().hex
    if runnable is not None:
        report = await orchestrator.scheduler.execute(
            runnable, principal=principal, idempotency_key=f"replay:{replay_id}",
            deadline_s=orchestrator.settings.execution_deadline_s,
            context=orchestrator._context_outputs(record),
        )
        replayed = {r.task_id: r for r in report.task_results}

    steps = []
    for task in plan.tasks:
        before = original.get(task.task_id)
        common = {"task_id": task.task_id, "tool_id": task.tool_id, "operation": task.operation,
                  "original_status": before.status.value if before else None,
                  "original_sha256": digest(before.output) if before and before.output is not None else None,
                  "original_ms": before.duration_ms if before else None}
        if task.task_id in unsafe:
            steps.append(StepReplay(outcome="not_rerun", note=f"Not re-run: {unsafe[task.task_id]}.", **common))
            continue
        after = replayed.get(task.task_id)
        if after is None:
            steps.append(StepReplay(outcome="failed", note="The step did not run.", **common))
            continue
        common.update(replay_status=after.status.value, replay_ms=after.duration_ms,
                      replay_sha256=digest(after.output) if after.output is not None else None)
        if after.status != TaskStatus.SUCCEEDED:
            why = after.error.message if after.error else after.status.value
            steps.append(StepReplay(outcome="failed", note=f"Now {after.status.value}: {why}", **common))
            continue
        if before is None or before.status != TaskStatus.SUCCEEDED:
            steps.append(StepReplay(outcome="differs", note="Succeeds now; it did not succeed originally.", **common))
            continue
        changed = _changed_paths(before.output, after.output)
        if changed:
            steps.append(StepReplay(outcome="differs", changed=changed,
                                    note=f"{len(changed)} value(s) changed" if "…" not in changed else "many values changed",
                                    **common))
        else:
            steps.append(StepReplay(outcome="identical", **common))

    counts = {k: sum(s.outcome == k for s in steps) for k in ("identical", "differs", "failed", "not_rerun")}
    if counts["failed"] or not (counts["identical"] + counts["differs"]):
        verdict = "incomplete"
    else:
        verdict = "differs" if counts["differs"] else "identical"

    receipt_ok = policy_changed = None
    if record.receipt is not None:
        findings = verify_against_record(record.receipt, record.public())
        receipt_ok = all(f.level == "ok" for f in findings)
        policy_changed = record.receipt.policy_sha256 != policy_digest(orchestrator.permissions.policy.grants)

    return ReplayReport(request_id=record.request_id, replay_id=replay_id, replayed_at=datetime.now(UTC).isoformat(),
                        verdict=verdict, steps=tuple(steps), counts=counts,
                        receipt_matches_record=receipt_ok, policy_changed=policy_changed)
