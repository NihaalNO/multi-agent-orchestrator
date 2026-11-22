"""Re-planning after plan-caused failures: rules, feedback content, reuse, and the bounded loop."""

import json

import pytest

from orchestrator.config import Settings
from orchestrator.contracts import (
    AccessLevel,
    ErrorInfo,
    ExecutionPlan,
    ExecutionReport,
    ExecutionStatus,
    PermissionGrant,
    Principal,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from orchestrator.core import Orchestrator, RequestState
from orchestrator.errors import PlannerUnavailableError
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import ApprovalManager, GrantPolicy, PermissionManager
from orchestrator.registry import ToolRegistry
from orchestrator.repair import build_feedback, repairable_failures, reusable_results, shape
from orchestrator.routing import RuleBasedPlanner, build_proposal
from orchestrator.tools import FileProcessingTool, PythonExecTool

from fakes import RecordsTool

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
GRANTS = [
    PermissionGrant(subject="role:analyst", tool_id="file_processing", resources=("file:*",)),
    PermissionGrant(subject="role:analyst", tool_id="python_exec", max_access=AccessLevel.EXECUTE),
    PermissionGrant(subject="role:analyst", tool_id="records", max_access=AccessLevel.WRITE),
]

LOAD = {"task_id": "load", "capability": "data.tabular.load", "tool_id": "file_processing",
        "operation": "read_csv", "inputs": {"path": "sales.csv"}}


def mean_task(column, task_id="avg"):
    code = f"vals = [float(r['{column}']) for r in inputs['rows']]\nresult = sum(vals) / len(vals)"
    return {"task_id": task_id, "capability": "data.compute", "tool_id": "python_exec", "operation": "run",
            "inputs": {"code": code, "inputs": {"rows": {"$ref": "load.rows"}}}, "depends_on": ["load"]}


class ScriptedLLMPlanner:
    """Stands in for an LLM planner: returns scripted plans in turn and records the feedback."""

    name = "scripted"
    supports_feedback = True

    def __init__(self, *plans, error_on_replan=None):
        self.plans, self.error_on_replan, self.feedback = list(plans), error_on_replan, []

    async def plan(self, request, catalog, feedback=None):
        self.feedback.append(feedback)
        if feedback is not None and self.error_on_replan:
            raise self.error_on_replan
        tasks = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        return build_proposal(request_id=request.request_id, planner=self.name, tasks=tasks)


@pytest.fixture
async def world(tmp_path):
    (tmp_path / "sales.csv").write_text("month,Revenue\njan,100\nfeb,300\n", encoding="utf-8")
    reg = ToolRegistry()
    await reg.register(FileProcessingTool(tmp_path))
    await reg.register(PythonExecTool(timeout_s=10))
    await reg.register(RecordsTool())
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    def orch(planner, **settings):
        settings.setdefault("llm_provider", "rule_based")
        return Orchestrator(reg, planner, PermissionManager(GrantPolicy(GRANTS), reg, audit),
                            ApprovalManager(audit), audit, settings=Settings(**settings))

    return reg, sink, orch


# --- The loop through the orchestrator -----------------------------------------------------------------


async def test_failed_plan_is_repaired_using_feedback(world):
    reg, sink, orch = world
    planner = ScriptedLLMPlanner([LOAD, mean_task("revenue")], [LOAD, mean_task("Revenue")])
    record = await orch(planner).submit("average revenue in sales.csv", ALICE)

    assert record.state == RequestState.COMPLETED and record.report.status == ExecutionStatus.SUCCEEDED
    assert record.report.final_output["avg"]["result"] == 200.0
    # what the planner was told: the real column names and the error, but no data values
    feedback = planner.feedback[1]
    assert "rows: list[2] of {month: string, Revenue: string}" in feedback and "KeyError" in feedback
    assert '\\"' not in feedback.split("inputs as planned")[0]  # shapes are not double-escaped
    assert "100" not in feedback.replace("list[2]", "") and "300" not in feedback
    # the unchanged load step was reused, not run twice
    assert [e.task_id for e in sink.events("task.started")].count("load") == 1
    assert len(record.replans) == 1 and "avg" in record.replans[0].failed
    assert any(n.startswith("The plan was revised 1 time(s)") for n in record.report.uncertainty)
    assert "Re-planned 1 time(s)" in record.summary
    assert {"replan_1", "execute_1", "resolve_1"} <= {s.stage for s in record.stages}
    assert record.executions == 2
    assert sink.events("request.replanning")


async def test_replanning_is_bounded(world):
    _, _, orch = world
    planner = ScriptedLLMPlanner([LOAD, mean_task("wrong")])  # keeps making the same mistake
    record = await orch(planner, max_replans=1).submit("x", ALICE)
    assert len(planner.feedback) == 2  # first plan + exactly one re-plan
    assert record.report.status == ExecutionStatus.PARTIAL and len(record.replans) == 1


async def test_replanning_can_be_disabled(world):
    _, _, orch = world
    planner = ScriptedLLMPlanner([LOAD, mean_task("wrong")])
    record = await orch(planner, max_replans=0).submit("x", ALICE)
    assert planner.feedback == [None] and record.replans == ()


async def test_planners_without_feedback_support_are_not_replanned(world, tmp_path):
    _, _, orch = world
    record = await orch(RuleBasedPlanner()).submit("calculate the average Missing in sales.csv", ALICE)
    assert record.report.status == ExecutionStatus.PARTIAL and record.replans == ()


async def test_replan_failure_is_reported_not_fatal(world):
    _, sink, orch = world
    planner = ScriptedLLMPlanner([LOAD, mean_task("wrong")], error_on_replan=PlannerUnavailableError("down"))
    record = await orch(planner).submit("x", ALICE)
    assert record.state == RequestState.COMPLETED and record.report.status == ExecutionStatus.PARTIAL
    assert any("did not succeed (planner_unavailable" in n for n in record.report.uncertainty)
    assert sink.events("request.replan_failed")


async def test_failed_writes_are_never_replanned(world):
    _, _, orch = world
    # The write's input refers to an upstream field that does not exist: a plan error, but on a write.
    bad_write = [LOAD, {"task_id": "w", "capability": "db.write", "tool_id": "records", "operation": "update",
                        "inputs": {"id": {"$ref": "load.missing_field"}}, "depends_on": ["load"]}]
    planner = ScriptedLLMPlanner(bad_write)
    record = await orch(planner).submit("x", ALICE)
    w = next(r for r in record.report.task_results if r.task_id == "w")
    assert w.status == TaskStatus.FAILED and w.error.code == "input_resolution_error"
    assert planner.feedback == [None] and record.replans == ()


# --- Rules -------------------------------------------------------------------------------------------------


def _plan(*tasks):
    return ExecutionPlan(request_id="r", tasks=tuple(TaskSpec(**t) for t in tasks))


def _report(*results):
    return ExecutionReport(request_id="r", execution_id="e", plan_id="p", status=ExecutionStatus.PARTIAL,
                           task_results=tuple(results))


def _failed(task_id, code="tool_execution_error", recoverable=False):
    return TaskResult(task_id=task_id, status=TaskStatus.FAILED,
                      error=ErrorInfo(code=code, message="boom", recoverable=recoverable))


@pytest.mark.parametrize(
    "results,expected",
    [
        ((_failed("avg"),), ["avg"]),
        ((_failed("avg", code="input_resolution_error"),), ["avg"]),
        ((_failed("avg", recoverable=True),), []),                       # transient: retries' job
        ((_failed("avg", code="tool_unavailable"),), []),                # not the plan's fault
        ((_failed("avg"), TaskResult(task_id="load", status=TaskStatus.DENIED)), []),            # permissions
        ((_failed("avg"), TaskResult(task_id="load", status=TaskStatus.AWAITING_APPROVAL)), []),  # approvals
        ((TaskResult(task_id="avg", status=TaskStatus.TIMED_OUT),), []),  # timeouts are not re-planned
    ],
)
async def test_repairable_failures_rules(world, results, expected):
    reg, _, _ = world
    plan = _plan(LOAD, mean_task("x"))
    assert [r.task_id for r in repairable_failures(plan, _report(*results), reg)] == expected


def test_shape_has_structure_but_no_values():
    out = {"rows": [{"month": "jan", "Revenue": "120000"}], "row_count": 6, "ok": True, "note": None}
    assert shape(out) == "{rows: list[1] of {month: string, Revenue: string}, row_count: number, ok: boolean, note: null}"


def test_build_feedback_lists_outcomes_errors_and_planned_inputs():
    plan = _plan(LOAD, mean_task("revenue"))
    tb = "Traceback ...\nKeyError: 'revenue'"
    report = _report(
        TaskResult(task_id="load", status=TaskStatus.SUCCEEDED, output={"rows": [{"Revenue": "1"}], "row_count": 1}),
        TaskResult(task_id="avg", status=TaskStatus.FAILED,
                   error=ErrorInfo(code="tool_execution_error", message="python code raised an error",
                                   details={"traceback": tb})),
    )
    fb = build_feedback(plan, report, [report.task_results[1]])
    assert "- load [data.tabular.load via file_processing.read_csv]: succeeded" in fb
    assert "KeyError: 'revenue'" in fb and "inputs as planned" in fb and "r['revenue']" in fb


def test_reusable_results_only_for_identical_calls():
    old = _plan(LOAD, mean_task("revenue"))
    report = _report(TaskResult(task_id="load", status=TaskStatus.SUCCEEDED, output={"rows": []}), _failed("avg"))
    renamed = {**LOAD, "task_id": "read_sales"}
    new = _plan(renamed, {**mean_task("Revenue"), "depends_on": ["read_sales"],
                          "inputs": {"code": "result = 1", "inputs": {"rows": {"$ref": "read_sales.rows"}}}})
    reuse = reusable_results(old, report, new)
    assert set(reuse) == {"read_sales"} and reuse["read_sales"].task_id == "read_sales"
    changed = _plan({**LOAD, "inputs": {"path": "other.csv"}}, mean_task("Revenue"))
    assert reusable_results(old, report, changed) == {}
