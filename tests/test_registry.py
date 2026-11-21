import asyncio

import pytest

from orchestrator.contracts import HealthStatus, OperationSpec, ToolSpec
from orchestrator.errors import (
    DuplicateToolError,
    InputValidationError,
    InvalidToolSpecError,
    OperationNotFoundError,
    OutputValidationError,
    ToolNotFoundError,
    ToolUnavailableError,
)
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.registry import ToolRegistry

from fakes import CalcTool, CrashingHealthTool, MissingHandlerTool, SlowHealthTool, make_spec


@pytest.fixture
def sink():
    return InMemoryAuditSink()


@pytest.fixture
def registry(sink):
    return ToolRegistry(AuditLog(sink), health_check_timeout_s=0.1)


async def test_register_and_discover_at_runtime(registry, sink):
    # Spec §9 scenario 1: a tool added while running is immediately discoverable.
    assert registry.find_by_capability("data.compute") == []
    await registry.register(CalcTool())
    matches = registry.find_by_capability("data.compute")
    assert [(m.tool_id, m.operation) for m in matches] == [("calc", "mean")]
    assert registry.capabilities() == {"data.compute": ["calc.mean"], "data.stats.mean": ["calc.mean"]}
    assert [e.kind for e in sink.events()] == ["tool.registered"]


async def test_duplicate_registration_rejected(registry):
    await registry.register(CalcTool())
    with pytest.raises(DuplicateToolError):
        await registry.register(CalcTool())


async def test_replace_keeps_enabled_flag_resets_health_and_closes_old(registry, sink):
    old = CalcTool()
    await registry.register(old)
    await registry.set_enabled("calc", False)
    await registry.check_health("calc")
    new = CalcTool(make_spec(version="2.0.0"))
    d = await registry.register(new, replace_existing=True)
    assert d.spec.version == "2.0.0"
    assert d.state.enabled is False
    assert d.state.health == HealthStatus.UNKNOWN
    assert old.closed and not new.closed
    assert "tool.replaced" in [e.kind for e in sink.events()]


async def test_unregister_removes_and_closes(registry):
    tool = CalcTool()
    await registry.register(tool)
    await registry.unregister("calc")
    assert "calc" not in registry and tool.closed
    with pytest.raises(ToolNotFoundError):
        registry.get("calc")
    with pytest.raises(ToolNotFoundError):
        await registry.unregister("calc")


async def test_missing_handler_rejected(registry):
    with pytest.raises(InvalidToolSpecError, match="without async handlers"):
        await registry.register(MissingHandlerTool())


async def test_invalid_json_schema_rejected(registry):
    spec = ToolSpec(
        tool_id="calc",
        name="c",
        description="c",
        operations=(
            OperationSpec(
                name="mean",
                description="m",
                capabilities=("data.compute",),
                input_schema={"type": "not-a-type"},
            ),
        ),
    )
    with pytest.raises(InvalidToolSpecError, match="invalid input schema"):
        await registry.register(CalcTool(spec))
    assert len(registry) == 0


async def test_disabled_tool_excluded_from_discovery(registry):
    await registry.register(CalcTool())
    await registry.set_enabled("calc", False)
    assert registry.find_by_capability("data.compute") == []
    assert registry.find_by_capability("data.compute", available_only=False)[0].available is False
    with pytest.raises(ToolUnavailableError, match="disabled"):
        registry.get_available("calc")
    await registry.set_enabled("calc", True)
    assert registry.get_available("calc").spec.tool_id == "calc"


@pytest.mark.parametrize(
    "tool,error_fragment",
    [
        (CalcTool(healthy=False), "returned False"),
        (CrashingHealthTool(), "backend down"),
        (SlowHealthTool(), "timed out"),
    ],
)
async def test_unhealthy_tools_detected_and_excluded(registry, sink, tool, error_fragment):
    await registry.register(tool)
    result = await registry.check_health()
    assert result == {"calc": HealthStatus.UNHEALTHY}
    state = registry.get("calc").state
    assert error_fragment in state.last_error
    assert registry.find_by_capability("data.compute") == []
    with pytest.raises(ToolUnavailableError, match="unhealthy"):
        registry.get_available("calc")
    assert sink.events("tool.unhealthy")


async def test_recovers_after_passing_health_check(registry):
    tool = CalcTool(healthy=False)
    await registry.register(tool)
    await registry.check_health()
    tool.healthy = True
    assert await registry.check_health() == {"calc": HealthStatus.HEALTHY}
    assert registry.find_by_capability("data.compute")


async def test_candidates_ranked_by_reliability_then_cost(registry):
    await registry.register(CalcTool(make_spec("cheap_flaky", reliability=0.3, cost=0)))
    await registry.register(CalcTool(make_spec("solid_pricey", reliability=0.9, cost=5)))
    await registry.register(CalcTool(make_spec("solid_cheap", reliability=0.9, cost=1)))
    order = [m.tool_id for m in registry.find_by_capability("data.compute")]
    assert order == ["solid_cheap", "solid_pricey", "cheap_flaky"]


async def test_input_and_output_validation(registry):
    await registry.register(CalcTool())
    registry.validate_input("calc", "mean", {"values": [1, 2]})
    with pytest.raises(InputValidationError) as exc:
        registry.validate_input("calc", "mean", {"values": "nope", "extra": 1})
    paths = {e["path"] for e in exc.value.details["errors"]}
    assert {"values", "<root>"} <= paths
    registry.validate_output("calc", "mean", {"mean": 1.5})
    with pytest.raises(OutputValidationError):
        registry.validate_output("calc", "mean", {"avg": 1.5})
    with pytest.raises(OperationNotFoundError):
        registry.validate_input("calc", "median", {})


async def test_concurrent_registrations_are_consistent(registry):
    tools = [CalcTool(make_spec(f"t{i}")) for i in range(50)]
    dupes = [CalcTool(make_spec("t0")) for _ in range(10)]
    results = await asyncio.gather(*(registry.register(t) for t in tools + dupes), return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(registry) == 50
    assert len(errors) == 10 and all(isinstance(e, DuplicateToolError) for e in errors)
    assert len(registry.capabilities()["data.compute"]) == 50


async def test_health_result_ignored_for_tool_replaced_mid_check(registry):
    slowish = CalcTool(healthy=False)

    async def slow_false():
        await asyncio.sleep(0.05)
        return False

    slowish.health_check = slow_false
    await registry.register(slowish)
    check = asyncio.create_task(registry.check_health())
    await asyncio.sleep(0.01)
    await registry.register(CalcTool(), replace_existing=True)
    assert await check == {}
    assert registry.get("calc").state.health == HealthStatus.UNKNOWN


async def test_adapter_invoke_dispatches_to_handler():
    from orchestrator.contracts import ExecutionContext, Principal

    ctx = ExecutionContext(request_id="r", execution_id="e", task_id="t", principal=Principal(user_id="u"))
    assert await CalcTool().invoke("mean", {"values": [2, 4]}, ctx) == {"mean": 3}
    with pytest.raises(OperationNotFoundError):
        await CalcTool().invoke("median", {}, ctx)
