import asyncio
import os
import subprocess
import time

import pytest

from orchestrator.contracts import (
    AccessLevel,
    ExecutionContext,
    ExecutionPlan,
    ExecutionStatus,
    PermissionGrant,
    PlanningRequest,
    Principal,
    TaskSpec,
    TaskStatus,
)
from orchestrator.contracts import TaskResult
from orchestrator.errors import InputResolutionError
from orchestrator.execution import (
    DagScheduler,
    ExecutionEngine,
    IdempotencyStore,
    ResourceLocks,
    overall_status,
    resolve,
)
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import ApprovalManager, GrantPolicy, PermissionManager
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RoutingService, RuleBasedPlanner
from orchestrator.tools import (
    FileProcessingTool,
    PythonExecTool,
    SearchResult,
    StaticSearchBackend,
    WebSearchTool,
)

from fakes import TESTER, WorkerTool, permissive, worker_spec


async def no_sleep(_):
    return None


class Harness:
    def __init__(self, registry, permissions=None, *, max_concurrency=8, retries_cap=3, approvals=None):
        self.registry = registry
        self.sink = InMemoryAuditSink()
        self.audit = AuditLog(self.sink)
        self.permissions = permissions or permissive(registry, self.audit)
        self.approvals = approvals or ApprovalManager(self.audit)
        self.engine = ExecutionEngine(
            registry, self.permissions, self.approvals, self.audit,
            max_concurrency=max_concurrency, max_retries_cap=retries_cap, sleep=no_sleep,
        )
        self.scheduler = DagScheduler(self.engine, self.audit)

    async def run(self, *tasks, principal=TESTER, **kw):
        plan = ExecutionPlan(request_id="req", tasks=tuple(tasks))
        return await self.scheduler.execute(plan, principal=principal, **kw)


def work(tid, deps=(), op="work", tool="worker", **inputs):
    return TaskSpec(task_id=tid, capability="test.work", tool_id=tool, operation=op, inputs=inputs, depends_on=deps)


def by_id(report):
    return {r.task_id: r for r in report.task_results}


@pytest.fixture
async def worker():
    return WorkerTool()


@pytest.fixture
async def h(worker):
    reg = ToolRegistry()
    await reg.register(worker)
    return Harness(reg)


# --- Concurrency & ordering -------------------------------------------------------------------


async def test_three_independent_tasks_run_concurrently(h, worker):
    # Spec §9 scenario 5.
    t0 = time.monotonic()
    report = await h.run(work("a", delay=0.3), work("b", delay=0.3), work("c", delay=0.3))
    elapsed = time.monotonic() - t0
    assert report.status == ExecutionStatus.SUCCEEDED
    assert worker.max_active == 3
    assert elapsed < 0.75, elapsed


async def test_per_tool_concurrency_limit(worker):
    reg = ToolRegistry()
    await reg.register(WorkerTool(worker_spec(max_concurrency=1)))
    h = Harness(reg)
    tool = reg.get("worker").adapter
    await h.run(work("a", delay=0.05), work("b", delay=0.05), work("c", delay=0.05))
    assert tool.max_active == 1


async def test_global_concurrency_limit(worker):
    reg = ToolRegistry()
    await reg.register(worker)
    h = Harness(reg, max_concurrency=2)
    await h.run(*(work(f"t{i}", delay=0.05) for i in range(5)))
    assert worker.max_active == 2


async def test_sequential_dependencies_pass_outputs(h, worker):
    # Spec §9 scenario 6.
    report = await h.run(
        work("c", deps=("b",), echo={"$ref": "b.echo.0"}),
        work("a", echo=["first", "x"]),
        work("b", echo={"$ref": "a.echo"}),
    )
    assert report.status == ExecutionStatus.SUCCEEDED
    starts = [label for kind, label, _ in worker.events if kind == "start"]
    assert starts == ["a", "b", "c"]
    assert by_id(report)["c"].output["echo"] == "first"


async def test_slow_branch_does_not_block_unrelated_branch(h, worker):
    await h.run(work("slow", delay=0.4), work("fast", delay=0.0), work("after_fast", deps=("fast",)))
    ends = {label: t for kind, label, t in worker.events if kind == "end"}
    assert ends["after_fast"] < ends["slow"]


# --- Timeouts & retries ---------------------------------------------------------------------------


async def test_timeout_is_retried_for_idempotent_ops_then_reported():
    # Spec §9 scenario 7.
    reg = ToolRegistry()
    tool = WorkerTool(worker_spec(timeout_s=0.1, max_retries=2))
    await reg.register(tool)
    h = Harness(reg)
    r = by_id(await h.run(work("slow", delay=5)))["slow"]
    assert r.status == TaskStatus.TIMED_OUT and r.attempts == 3
    assert r.error.code == "tool_timeout"
    assert tool.cancelled == ["slow"] * 3
    assert len(h.sink.events("task.retry")) == 2


async def test_transient_failure_recovers(h, worker):
    r = by_id(await h.run(work("a", fail_transient=2)))["a"]
    assert r.status == TaskStatus.SUCCEEDED and r.attempts == 3


async def test_retry_cap_overrides_tool_setting(worker):
    reg = ToolRegistry()
    await reg.register(worker)
    h = Harness(reg, retries_cap=1)
    r = by_id(await h.run(work("a", fail_transient=5)))["a"]
    assert r.status == TaskStatus.FAILED and r.attempts == 2 and r.error.recoverable


async def test_non_recoverable_errors_are_not_retried(h, worker):
    rep = by_id(await h.run(work("hard", fail_hard=True), work("bug", crash=True)))
    assert rep["hard"].attempts == 1 and rep["hard"].status == TaskStatus.FAILED
    assert rep["bug"].attempts == 1 and "adapter bug" in rep["bug"].error.message


async def test_non_idempotent_retried_on_transient_but_not_on_timeout():
    reg = ToolRegistry()
    tool = WorkerTool(worker_spec(timeout_s=0.1, max_retries=2))
    await reg.register(tool)
    h = Harness(reg)
    rep = by_id(await h.run(work("t", op="write", fail_transient=1), work("slow", op="write", delay=5)))
    assert rep["t"].status == TaskStatus.SUCCEEDED and rep["t"].attempts == 2
    assert rep["slow"].status == TaskStatus.TIMED_OUT and rep["slow"].attempts == 1


# --- Failure propagation ----------------------------------------------------------------------------


async def test_partial_failure_skips_dependents_and_keeps_independent_work(h):
    # Spec §9 scenario 10.
    report = await h.run(
        work("bad", fail_hard=True),
        work("after_bad", deps=("bad",)),
        work("after_after", deps=("after_bad",)),
        work("ok"),
    )
    rep = by_id(report)
    assert rep["bad"].status == TaskStatus.FAILED
    assert rep["after_bad"].status == rep["after_after"].status == TaskStatus.SKIPPED
    assert "bad" in rep["after_bad"].error.message
    assert rep["ok"].status == TaskStatus.SUCCEEDED
    assert report.status == ExecutionStatus.PARTIAL
    assert set(report.final_output) == {"ok"}


async def test_unresolvable_ref_fails_task(h):
    rep = by_id(await h.run(work("a"), work("b", deps=("a",), x={"$ref": "a.missing"}), work("c", deps=("b",))))
    assert rep["b"].status == TaskStatus.FAILED and rep["b"].error.code == "input_resolution_error"
    assert rep["c"].status == TaskStatus.SKIPPED


async def test_output_schema_violation_fails_task(h):
    r = by_id(await h.run(work("x", op="bad_output")))["x"]
    assert r.status == TaskStatus.FAILED and r.error.code == "output_validation_error"


async def test_tool_disabled_after_planning(h):
    await h.registry.set_enabled("worker", False)
    r = by_id(await h.run(work("a")))["a"]
    assert r.status == TaskStatus.FAILED and r.error.code == "tool_unavailable"


async def test_unbound_plan_rejected(h):
    with pytest.raises(ValueError, match="unbound"):
        await h.run(TaskSpec(task_id="a", capability="test.work"))


# --- Permissions at execution time --------------------------------------------------------------


def policy_manager(registry, grants, audit=None):
    return PermissionManager(GrantPolicy(grants), registry, audit)


async def test_final_check_uses_resolved_resource(worker):
    reg = ToolRegistry()
    await reg.register(worker)
    grants = [PermissionGrant(subject="role:tester", tool_id="worker", max_access=AccessLevel.WRITE,
                              resources=("res:allowed",))]
    h = Harness(reg, policy_manager(reg, grants))
    # Upstream output decides the resource; it resolves to a forbidden one.
    report = await h.run(
        work("pick", echo="forbidden", key="allowed"),
        work("write", op="write", deps=("pick",), key={"$ref": "pick.echo"}),
    )
    rep = by_id(report)
    assert rep["pick"].status == TaskStatus.SUCCEEDED
    assert rep["write"].status == TaskStatus.DENIED
    assert ("write", 1) not in [(lbl, a) for lbl, a in worker.calls if lbl == "write"]


async def test_revoked_permission_blocks_execution(worker):
    reg = ToolRegistry()
    await reg.register(worker)
    h = Harness(reg, policy_manager(reg, []))  # nothing granted at execution time
    r = by_id(await h.run(work("a")))["a"]
    assert r.status == TaskStatus.DENIED and worker.calls == []


async def test_sensitive_operation_approval_flow():
    from fakes import RecordsTool

    reg = ToolRegistry()
    records = RecordsTool()
    await reg.register(records)
    grants = [PermissionGrant(subject="role:tester", tool_id="records", max_access=AccessLevel.WRITE,
                              allow_sensitive=True)]
    h = Harness(reg, policy_manager(reg, grants))
    delete = TaskSpec(task_id="del", capability="db.delete", tool_id="records", operation="delete", inputs={"id": 1})

    first = await h.run(delete)
    r = by_id(first)["del"]
    assert first.status == ExecutionStatus.AWAITING_APPROVAL
    assert r.status == TaskStatus.AWAITING_APPROVAL and r.approval_id and 1 in records.data

    approver = Principal(user_id="boss", roles=frozenset({"approver"}))
    await h.approvals.decide(r.approval_id, approver=approver, approve=True)
    second = await h.run(delete, approvals={"del": r.approval_id})
    assert by_id(second)["del"].status == TaskStatus.SUCCEEDED and 1 not in records.data

    again = await h.run(delete, approvals={"del": r.approval_id})
    assert by_id(again)["del"].status == TaskStatus.DENIED  # single use


async def test_unapproved_approval_keeps_task_waiting():
    from fakes import RecordsTool

    reg = ToolRegistry()
    await reg.register(RecordsTool())
    grants = [PermissionGrant(subject="role:tester", max_access=AccessLevel.WRITE, allow_sensitive=True)]
    h = Harness(reg, policy_manager(reg, grants))
    delete = TaskSpec(task_id="del", capability="db.delete", tool_id="records", operation="delete", inputs={"id": 1})
    aid = by_id(await h.run(delete))["del"].approval_id
    r = by_id(await h.run(delete, approvals={"del": aid}))["del"]
    assert r.status == TaskStatus.AWAITING_APPROVAL


# --- Idempotency & locking -------------------------------------------------------------------------


async def test_duplicate_requests_execute_once(h, worker):
    # Spec §9 scenario 9.
    write = work("w", op="write", key="k1", delay=0.1, echo="v")
    a, b = await asyncio.gather(h.run(write, idempotency_key="idem-1"), h.run(write, idempotency_key="idem-1"))
    assert [c for c in worker.calls if c[0] == "w"] == [("w", 1)]
    results = [by_id(a)["w"], by_id(b)["w"]]
    assert all(r.status == TaskStatus.SUCCEEDED for r in results)
    assert sorted(r.deduplicated for r in results) == [False, True]

    later = by_id(await h.run(write, idempotency_key="idem-1"))["w"]
    assert later.deduplicated and len(worker.calls) == 1
    await h.run(write, idempotency_key="idem-2")
    await h.run(write)  # no key: no deduplication
    assert len(worker.calls) == 3


async def test_failed_calls_are_not_cached(h, worker):
    bad = work("w", op="write", fail_hard=True)
    await h.run(bad, idempotency_key="k")
    await h.run(bad, idempotency_key="k")
    assert len(worker.calls) == 2


async def test_writes_to_same_resource_are_serialised(h, worker):
    await h.run(
        work("w1", op="write", key="same", delay=0.1),
        work("w2", op="write", key="same", delay=0.1),
        work("w3", op="write", key="other", delay=0.1),
    )
    assert worker.max_by_resource == {"res:same": 1, "res:other": 1}
    assert worker.max_active == 2  # different resources still run in parallel


async def test_resource_locks_are_released():
    locks = ResourceLocks()
    async with locks.hold("a"):
        assert len(locks) == 1
    assert len(locks) == 0


async def test_idempotency_store_retries_after_cancelled_original():
    store = IdempotencyStore()
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)

    async def fast():
        return TaskResult(task_id="t", status=TaskStatus.SUCCEEDED, output={})

    first = asyncio.create_task(store.run_once("k", slow))
    await started.wait()
    second = asyncio.create_task(store.run_once("k", fast))
    await asyncio.sleep(0.01)
    first.cancel()
    result = await second
    assert result.status == TaskStatus.SUCCEEDED and not result.deduplicated


# --- Deadline & cancellation -----------------------------------------------------------------------


async def test_deadline_cancels_running_and_pending_tasks(h, worker):
    report = await h.run(work("long", delay=5), work("after", deps=("long",)), work("quick"), deadline_s=0.3)
    rep = by_id(report)
    assert rep["quick"].status == TaskStatus.SUCCEEDED
    assert rep["long"].status == rep["after"].status == TaskStatus.CANCELLED
    assert rep["long"].error.code == "execution_deadline"
    assert worker.cancelled == ["long"]
    assert report.status == ExecutionStatus.PARTIAL


async def test_caller_cancellation_propagates_to_tools(h, worker):
    run = asyncio.create_task(h.run(work("a", delay=5), work("b", delay=5)))
    await asyncio.sleep(0.1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert sorted(worker.cancelled) == ["a", "b"] and worker.active == 0


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancelling_python_task_kills_child_process(tmp_path):
    tool = PythonExecTool(timeout_s=30)
    pid_file = tmp_path / "pid.txt"
    code = "import os, time\nopen(inputs, 'w').write(str(os.getpid()))\nwhile True: time.sleep(0.05)"
    ctx = ExecutionContext(request_id="r", execution_id="e", task_id="t", principal=TESTER)
    task = asyncio.create_task(tool.invoke("run", {"code": code, "inputs": str(pid_file)}, ctx))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    assert _pid_alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _pid_alive(pid)


# --- Helpers -----------------------------------------------------------------------------------------


def test_resolve_refs():
    outputs = {"a": {"rows": [{"x": 1}, {"x": 2}], "n": 3}}
    assert resolve({"v": {"$ref": "a.rows.1.x"}, "all": [{"$ref": "a.n"}], "lit": 5}, outputs) == {
        "v": 2, "all": [3], "lit": 5,
    }
    copied = resolve({"$ref": "a.rows"}, outputs)
    copied.append("mutated")
    assert len(outputs["a"]["rows"]) == 2
    for bad in ("a.rows.9", "a.missing", "a.n.deeper", "ghost"):
        with pytest.raises(InputResolutionError):
            resolve({"$ref": bad}, outputs)


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([TaskStatus.SUCCEEDED], ExecutionStatus.SUCCEEDED),
        ([TaskStatus.SUCCEEDED, TaskStatus.FAILED], ExecutionStatus.PARTIAL),
        ([TaskStatus.FAILED, TaskStatus.SKIPPED], ExecutionStatus.FAILED),
        ([TaskStatus.SUCCEEDED, TaskStatus.AWAITING_APPROVAL, TaskStatus.SKIPPED], ExecutionStatus.AWAITING_APPROVAL),
    ],
)
def test_overall_status(statuses, expected):
    assert overall_status([TaskResult(task_id=f"t{i}", status=s) for i, s in enumerate(statuses)]) == expected


# --- MVP end to end: plan -> route -> authorize -> execute -------------------------------------------


async def test_mvp_request_end_to_end(tmp_path):
    (tmp_path / "sales.csv").write_text("month,Revenue\njan,100\nfeb,200\nmar,300\n", encoding="utf-8")
    reg = ToolRegistry()
    await reg.register(FileProcessingTool(tmp_path))
    await reg.register(PythonExecTool(timeout_s=10))
    await reg.register(WebSearchTool(StaticSearchBackend([
        SearchResult(title="Industry revenue benchmarks 2026", url="https://example.com/b", snippet="median revenue 180"),
    ])))
    h = Harness(reg)
    outcome = await RoutingService(reg, RuleBasedPlanner(), h.permissions, h.audit).plan_and_route(
        PlanningRequest(
            request_id="mvp",
            text="Read the uploaded CSV file sales.csv, calculate its average revenue using Python, "
                 "and independently search for the latest industry revenue benchmarks.",
        ),
        TESTER,
    )
    report = await h.scheduler.execute(outcome.routed.plan, principal=TESTER)
    assert report.status == ExecutionStatus.SUCCEEDED, report
    assert report.final_output["compute_mean"]["result"] == {"column": "Revenue", "mean": 200.0, "count": 3}
    assert report.final_output["web_search"]["results"][0]["url"] == "https://example.com/b"
    kinds = {e.kind for e in h.sink.events()}
    assert {"routing.decision", "permission.decision", "execution.started", "task.finished", "execution.finished"} <= kinds
