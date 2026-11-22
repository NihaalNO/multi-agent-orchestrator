from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.contracts import (
    ConflictKind,
    ExecutionPlan,
    ExecutionReport,
    ExecutionStatus,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from orchestrator.execution import DagScheduler, ExecutionEngine
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import ApprovalManager, args_digest
from orchestrator.registry import ToolRegistry
from orchestrator.resolution import ConflictResolver, ResolverConfig, flatten, normalize, normalize_url, source_time

from fakes import TESTER, CalcTool, WorkerTool, make_spec, permissive, worker_spec

NOW = datetime(2026, 11, 22, tzinfo=UTC)


@pytest.fixture
async def registry():
    reg = ToolRegistry()
    for tool_id, rel in (("src_high", 0.9), ("src_mid", 0.6), ("src_low", 0.5), ("src_low2", 0.5)):
        await reg.register(CalcTool(make_spec(tool_id, reliability=rel)))
    await reg.register(WorkerTool())
    return reg


@pytest.fixture
def sink():
    return InMemoryAuditSink()


@pytest.fixture
def resolver(registry, sink):
    return ConflictResolver(registry, AuditLog(sink), clock=lambda: NOW)


def task(tid, group=None, deps=(), tool="src_low", op="mean", cap="data.compute"):
    return TaskSpec(task_id=tid, capability=cap, tool_id=tool, operation=op, compare_group=group, depends_on=deps)


def ok(tid, output, tool="src_low", op="mean", **kw):
    return TaskResult(task_id=tid, tool_id=tool, operation=op, status=TaskStatus.SUCCEEDED, output=output, **kw)


def report(*results, status=ExecutionStatus.SUCCEEDED):
    return ExecutionReport(request_id="r", execution_id="e", plan_id="p", status=status, task_results=tuple(results))


def resolve(resolver, tasks, results):
    plan = ExecutionPlan(request_id="r", tasks=tuple(tasks))
    return resolver.resolve(plan, report(*results))


def kinds(rep):
    return [c.kind for c in rep.conflicts]


# --- Cross-checks -------------------------------------------------------------------------------


def test_agreeing_sources_produce_no_conflicts(resolver):
    rep = resolve(resolver, [task("a", "avg"), task("b", "avg")],
                  [ok("a", {"mean": 200, "n": 3}), ok("b", {"mean": 200.0000000001, "n": 3})])
    assert rep.conflicts == ()
    assert rep.resolved["avg"] == {"mean": 200, "n": 3}
    assert not rep.requires_review and rep.uncertainty == ()


def test_format_mismatch_is_resolved_by_normalization(resolver):
    rep = resolve(resolver, [task("a", "avg"), task("b", "avg")],
                  [ok("a", {"mean": "200"}), ok("b", {"mean": 200})])
    [c] = rep.conflicts
    assert c.kind == ConflictKind.FORMAT_MISMATCH and c.resolved and c.policy == "normalization"
    assert rep.resolved["avg"]["mean"] == 200.0


def test_majority_wins_with_three_sources(resolver):
    rep = resolve(resolver, [task("a", "g"), task("b", "g"), task("c", "g")],
                  [ok("a", {"mean": 200}), ok("b", {"mean": 250}), ok("c", {"mean": 200})])
    [c] = rep.conflicts
    assert c.kind == ConflictKind.CONTRADICTION and c.policy == "majority" and c.value == 200
    assert len(c.candidates) == 3
    assert "accepted 200" in rep.uncertainty[0]


def test_recency_prefers_newest_source(resolver):
    rep = resolve(resolver, [task("old", "g"), task("new", "g")], [
        ok("old", {"rate": 4.5, "as_of": "2026-01-01"}),
        ok("new", {"rate": 4.75, "as_of": "2026-06-01"}),
    ])
    c = next(c for c in rep.conflicts if c.field == "rate")
    assert c.policy == "recency" and c.chosen_task_id == "new" and c.value == 4.75


def test_source_priority_when_reliability_clearly_differs(resolver):
    rep = resolve(resolver, [task("hi", "g", tool="src_high"), task("lo", "g")],
                  [ok("hi", {"mean": 1}, tool="src_high"), ok("lo", {"mean": 2})])
    [c] = rep.conflicts
    assert c.policy == "source_priority" and c.chosen_task_id == "hi" and not c.escalated


def test_insufficient_evidence_is_escalated_not_fabricated(resolver):
    # Spec §9 scenario 8: contradictory outputs, no policy applies -> escalate, value unknown.
    rep = resolve(resolver, [task("a", "g", tool="src_mid"), task("b", "g")],
                  [ok("a", {"mean": 1, "n": 3}, tool="src_mid"), ok("b", {"mean": 2, "n": 3})])
    [c] = rep.conflicts
    assert c.kind == ConflictKind.CONTRADICTION and c.escalated and not c.resolved
    assert c.value is None and {x.value for x in c.candidates} == {1, 2}
    assert rep.resolved["g"] == {"mean": None, "n": 3}  # agreed fields kept, disputed one unknown
    assert rep.requires_review
    assert "insufficient" in rep.uncertainty[0]


def test_policies_can_be_restricted(registry):
    strict = ConflictResolver(registry, config=ResolverConfig(policies=("normalization",)), clock=lambda: NOW)
    rep = resolve(strict, [task("hi", "g", tool="src_high"), task("lo", "g")],
                  [ok("hi", {"mean": 1}, tool="src_high"), ok("lo", {"mean": 2})])
    assert rep.conflicts[0].escalated


def test_cross_check_needs_two_results(resolver):
    rep = resolve(resolver, [task("a", "g"), task("b", "g")], [
        ok("a", {"mean": 1}),
        TaskResult(task_id="b", tool_id="src_low", operation="mean", status=TaskStatus.FAILED),
    ])
    assert any("only 1 of 2" in n for n in rep.uncertainty)
    assert ConflictKind.PARTIAL_FAILURE in kinds(rep)


def test_outputs_without_common_fields_are_reported(resolver):
    rep = resolve(resolver, [task("a", "g"), task("b", "g")], [ok("a", {"mean": 1}), ok("b", {"avg": 1})])
    assert any("no fields in common" in n for n in rep.uncertainty)


def test_volatile_fields_are_ignored(resolver):
    rep = resolve(resolver, [task("a", "g"), task("b", "g")], [
        ok("a", {"result": 5, "stdout": "x", "retrieved_at": "2026-01-01"}),
        ok("b", {"result": 5, "stdout": "y", "retrieved_at": "2026-02-01"}),
    ])
    assert rep.conflicts == ()


# --- Partial failure, duplicates, merging ----------------------------------------------------------


def test_partial_failure_is_reported(resolver):
    from orchestrator.contracts import ErrorInfo

    rep = resolve(resolver, [task("a"), task("b")], [
        ok("a", {"mean": 1}),
        TaskResult(task_id="b", status=TaskStatus.TIMED_OUT, error=ErrorInfo(code="tool_timeout", message="slow")),
    ])
    [c] = rep.conflicts
    assert c.kind == ConflictKind.PARTIAL_FAILURE and c.task_ids == ("b",)
    assert "timed_out: slow" in c.description and "incomplete" in rep.uncertainty[0]


def test_identical_duplicate_calls_are_merged(resolver):
    d = args_digest({"values": [1, 2]})
    rep = resolve(resolver, [task("a"), task("b")],
                  [ok("a", {"mean": 1.5}, args_digest=d), ok("b", {"mean": 1.5}, args_digest=d)])
    [c] = rep.conflicts
    assert c.kind == ConflictKind.DUPLICATE and c.resolved and c.policy == "identical_results"


def test_duplicate_calls_with_different_outputs_are_cross_checked(resolver):
    d = args_digest({"q": 1})
    rep = resolve(resolver, [task("a"), task("b")],
                  [ok("a", {"mean": 1}, args_digest=d), ok("b", {"mean": 2}, args_digest=d)])
    assert kinds(rep) == [ConflictKind.DUPLICATE, ConflictKind.CONTRADICTION]
    assert rep.conflicts[1].escalated  # same tool, same reliability: nothing to choose by


def test_search_results_are_merged_by_normalised_url(resolver):
    tasks = [task("s1", tool="src_high", op="mean", cap="web.search"), task("s2", cap="web.search")]
    results = [
        ok("s1", {"results": [{"url": "https://Example.com/a/", "title": "A"},
                              {"url": "https://example.com/b", "title": "B"}]}, tool="src_high"),
        ok("s2", {"results": [{"url": "https://example.com/a?utm_source=x#top", "title": "A again"},
                              {"url": "https://example.com/c", "title": "C"}]}),
    ]
    rep = resolve(resolver, tasks, results)
    merged = rep.resolved["merged:web.search"]
    assert [m["title"] for m in merged] == ["A", "B", "C"]  # most reliable source's copy kept
    assert merged[0]["found_by"] == ["s1", "s2"]
    [c] = rep.conflicts
    assert c.kind == ConflictKind.DUPLICATE and c.policy == "merged_by_url"


# --- Concurrent writes & staleness -----------------------------------------------------------------


def _write(tid, resource="res:k"):
    return ok(tid, {"ok": True}, tool="worker", op="write", resource=resource)


def test_unordered_writes_to_same_resource_are_escalated(resolver):
    tasks = [task("w1", tool="worker", op="write"), task("w2", tool="worker", op="write"),
             task("w3", tool="worker", op="write")]
    rep = resolve(resolver, tasks, [_write("w1"), _write("w2"), _write("w3", resource="res:other")])
    [c] = rep.conflicts
    assert c.kind == ConflictKind.CONCURRENT_WRITE and c.escalated and c.task_ids == ("w1", "w2")
    assert rep.requires_review


def test_dependency_ordered_writes_are_fine(resolver):
    tasks = [task("w1", tool="worker", op="write"), task("w2", tool="worker", op="write", deps=("w1",))]
    rep = resolve(resolver, tasks, [_write("w1"), _write("w2")])
    [c] = rep.conflicts
    assert c.resolved and c.policy == "dependency_order" and not c.escalated


def test_reads_of_same_resource_are_not_write_conflicts(resolver):
    tasks = [task("r1", tool="worker", op="work"), task("r2", tool="worker", op="work")]
    rep = resolve(resolver, tasks, [ok("r1", {"x": 1}, tool="worker", op="work", resource="res:k"),
                                    ok("r2", {"y": 1}, tool="worker", op="work", resource="res:k")])
    assert rep.conflicts == ()


def test_stale_information_is_flagged(resolver, registry):
    old = (NOW - timedelta(days=800)).date().isoformat()
    out = {"results": [{"url": "https://e.com/1", "published_at": old}, {"url": "https://e.com/2", "published_at": "2026-10-01"}]}
    rep = resolve(resolver, [task("s", cap="web.search")], [ok("s", out)])
    stale = [c for c in rep.conflicts if c.kind == ConflictKind.STALE]
    assert len(stale) == 1 and old in stale[0].description and not stale[0].escalated
    assert any("outdated" in n for n in rep.uncertainty)

    lenient = ConflictResolver(registry, config=ResolverConfig(stale_after=None), clock=lambda: NOW)
    assert not [c for c in resolve(lenient, [task("s", cap="web.search")], [ok("s", out)]).conflicts
                if c.kind == ConflictKind.STALE]


def test_conflicts_are_audited(resolver, sink):
    resolve(resolver, [task("a", "g", tool="src_mid"), task("b", "g")],
            [ok("a", {"mean": 1}, tool="src_mid"), ok("b", {"mean": 2})])
    [event] = sink.events("conflict.escalated")
    assert event.data["field"] == "mean" and event.data["conflict_kind"] == "contradiction"


def test_input_report_is_not_modified(resolver):
    original = report(ok("a", {"mean": 1}), ok("b", {"mean": 2}))
    plan = ExecutionPlan(request_id="r", tasks=(task("a", "g"), task("b", "g")))
    resolver.resolve(plan, original)
    assert original.conflicts == () and original.resolved == {}


# --- End to end through the scheduler ---------------------------------------------------------------


async def test_conflicting_tools_end_to_end():
    reg = ToolRegistry()
    await reg.register(WorkerTool(worker_spec("worker_a")))
    await reg.register(WorkerTool(worker_spec("worker_b")))
    audit = AuditLog()
    engine = ExecutionEngine(reg, permissive(reg, audit), ApprovalManager(audit), audit)
    plan = ExecutionPlan(request_id="r", tasks=(
        TaskSpec(task_id="a", capability="test.work", tool_id="worker_a", operation="work",
                 inputs={"label": "x", "echo": 41}, compare_group="answer"),
        TaskSpec(task_id="b", capability="test.work", tool_id="worker_b", operation="work",
                 inputs={"label": "x", "echo": 42}, compare_group="answer"),
    ))
    executed = await DagScheduler(engine, audit).execute(plan, principal=TESTER)
    assert all(r.args_digest for r in executed.task_results)
    final = ConflictResolver(reg, audit).resolve(plan, executed)
    [c] = final.conflicts
    assert c.field == "echo" and c.escalated
    assert final.resolved["answer"] == {"label": "x", "echo": None}
    assert final.status == ExecutionStatus.SUCCEEDED and final.requires_review


# --- Value helpers --------------------------------------------------------------------------------


def test_flatten_and_normalize():
    assert flatten({"a": {"b": [1, {"c": 2}]}, "e": {}, "stdout": "x"}) == {"a.b.0": 1, "a.b.1.c": 2, "e": {}}
    assert flatten(5) == {"<value>": 5}
    assert normalize(" 2.50 ") == 2.5 and normalize(3) == 3.0 and normalize(True) is True
    assert normalize("Two") == "Two"


@pytest.mark.parametrize(
    "a,b",
    [
        ("HTTPS://Example.COM/x/", "https://example.com/x"),
        ("https://example.com:443/x?b=2&a=1", "https://example.com/x?a=1&b=2"),
        ("https://example.com/x?utm_campaign=z&id=3#frag", "https://example.com/x?id=3"),
    ],
)
def test_normalize_url(a, b):
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_keeps_meaningful_differences():
    assert normalize_url("https://example.com/x?id=3") != normalize_url("https://example.com/x?id=4")
    assert normalize_url("http://example.com/x") != normalize_url("https://example.com/x")


def test_source_time_prefers_shallowest_timestamp():
    out = {"as_of": "2026-05-01", "items": [{"published_at": "2026-09-01"}]}
    assert source_time(out) == datetime(2026, 5, 1, tzinfo=UTC)
    assert source_time({"x": 1}) is None
