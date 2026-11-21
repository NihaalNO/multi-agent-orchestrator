import pytest

from orchestrator.contracts import PlanProposal
from orchestrator.errors import InputValidationError, NoMatchingToolError
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.registry import ToolRegistry
from orchestrator.routing import CapabilityRouter, build_proposal

from fakes import CalcTool, make_spec


def proposal(*tasks, unsupported=()):
    return build_proposal(request_id="r1", planner="test", tasks=list(tasks), unsupported=unsupported)


def task(tid, cap="data.compute", deps=(), **kw):
    return {"task_id": tid, "capability": cap, "depends_on": deps, "inputs": kw.pop("inputs", {"values": [1, 2]}), **kw}


@pytest.fixture
async def registry():
    reg = ToolRegistry()
    await reg.register(CalcTool(make_spec("calc_good", reliability=0.9)))
    await reg.register(CalcTool(make_spec("calc_meh", reliability=0.4)))
    return reg


@pytest.fixture
def sink():
    return InMemoryAuditSink()


@pytest.fixture
def router(registry, sink):
    return CapabilityRouter(registry, AuditLog(sink))


def test_selects_best_candidate_and_binds_plan(router, sink):
    routed = router.route(proposal(task("a")))
    [d] = routed.decisions
    assert (d.tool_id, d.operation) == ("calc_good", "mean")
    assert d.alternatives == ("calc_meh.mean",)
    assert routed.plan.task("a").tool_id == "calc_good"
    assert routed.complete
    assert sink.events("routing.decision")[0].data["rationale"].startswith("best of 2")


def test_valid_proposal_is_kept_even_if_not_top_ranked(router):
    routed = router.route(proposal(task("a", tool_id="calc_meh", operation="mean")))
    [d] = routed.decisions
    assert d.tool_id == "calc_meh" and d.proposal_rejected is None
    assert "verified" in d.rationale


@pytest.mark.parametrize(
    "tool_id,operation,fragment",
    [
        ("ghost", "mean", "not registered"),
        ("calc_meh", "median", "no operation"),
    ],
)
def test_invalid_proposal_is_overridden_with_reason(router, tool_id, operation, fragment):
    routed = router.route(proposal(task("a", tool_id=tool_id, operation=operation)))
    [d] = routed.decisions
    assert d.tool_id == "calc_good"
    assert fragment in d.proposal_rejected
    assert d.proposed == f"{tool_id}.{operation}"


async def test_proposal_for_wrong_capability_is_overridden(registry, router):
    from fakes import CalcTool as _C
    from orchestrator.contracts import OperationSpec, ToolSpec

    other = ToolSpec(
        tool_id="echo", name="e", description="e",
        operations=(OperationSpec(name="mean", description="m", capabilities=("text.echo",)),),
    )
    await registry.register(_C(other))
    routed = router.route(proposal(task("a", tool_id="echo", operation="mean")))
    assert "does not provide capability" in routed.decisions[0].proposal_rejected
    assert routed.decisions[0].tool_id == "calc_good"


async def test_disabled_proposal_is_overridden(registry, router):
    await registry.set_enabled("calc_meh", False)
    routed = router.route(proposal(task("a", tool_id="calc_meh", operation="mean")))
    assert "unavailable" in routed.decisions[0].proposal_rejected


def test_no_matching_tool(router, sink):
    # Spec §9 scenario 3.
    with pytest.raises(NoMatchingToolError) as exc:
        router.route(proposal(task("a", cap="video.render")))
    [u] = exc.value.details["unrouted"]
    assert u["task_id"] == "a" and "no registered tool provides" in u["reason"]
    assert sink.events("routing.failed")


async def test_reason_distinguishes_unavailable_providers(registry, router):
    await registry.set_enabled("calc_good", False)
    await registry.set_enabled("calc_meh", False)
    with pytest.raises(NoMatchingToolError) as exc:
        router.route(proposal(task("a")))
    assert "disabled or unhealthy" in exc.value.details["unrouted"][0]["reason"]


def test_planner_unsupported_parts_fail_strict_routing(router):
    with pytest.raises(NoMatchingToolError) as exc:
        router.route(proposal(task("a"), unsupported=["send an email"]))
    assert exc.value.details["unsupported"] == ["send an email"]


def test_partial_routing_drops_unroutable_tasks_and_dependents(router):
    p = proposal(
        task("ok"),
        task("bad", cap="video.render"),
        task("after_bad", deps=("bad",)),
        task("after_ok", deps=("ok",)),
    )
    routed = router.route(p, allow_partial=True)
    assert [t.task_id for t in routed.plan.tasks] == ["ok", "after_ok"]
    assert {u.task_id: u.reason.split(" ")[0] for u in routed.unrouted} == {"bad": "no", "after_bad": "depends"}
    assert not routed.complete


def test_partial_routing_with_nothing_routable(router):
    routed = router.route(proposal(task("bad", cap="video.render")), allow_partial=True)
    assert routed.plan is None and routed.decisions == ()


def test_static_inputs_are_validated_at_routing_time(router):
    with pytest.raises(InputValidationError) as exc:
        router.route(proposal(task("a", inputs={"values": "not-a-list"})))
    assert exc.value.details["task_id"] == "a"


def test_inputs_with_refs_are_deferred_to_execution(router):
    p = proposal(task("a"), task("b", inputs={"values": {"$ref": "a.mean"}}))
    routed = router.route(p)
    assert routed.plan.layers() == [["a"], ["b"]]


def test_candidate_filter_excludes_tools(registry):
    router = CapabilityRouter(registry, candidate_filter=lambda m: m.tool_id != "calc_good")
    routed = router.route(proposal(task("a")))
    assert routed.decisions[0].tool_id == "calc_meh"
    blocked = CapabilityRouter(registry, candidate_filter=lambda m: False)
    with pytest.raises(NoMatchingToolError) as exc:
        blocked.route(proposal(task("a")))
    assert "no permitted provider" in exc.value.details["unrouted"][0]["reason"]


def test_empty_proposal_is_rejected(router):
    with pytest.raises(NoMatchingToolError):
        router.route(PlanProposal(request_id="r", plan=None, unsupported=("everything",), planner="t"))
