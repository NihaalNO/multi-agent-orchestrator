import pytest
from pydantic import ValidationError

from orchestrator.config import Settings
from orchestrator.contracts import (
    AccessLevel,
    ErrorInfo,
    ExecutionPlan,
    OperationSpec,
    TaskSpec,
    ToolSpec,
)
from orchestrator.errors import (
    ConfigurationError,
    PlanCycleError,
    ToolTimeoutError,
    UnknownDependencyError,
)

from fakes import make_spec


# --- ToolSpec ------------------------------------------------------------------------


def test_tool_spec_exposes_capabilities_and_timeouts():
    spec = make_spec()
    assert spec.capabilities == {"data.compute", "data.stats.mean"}
    assert spec.operation("mean") is not None
    assert spec.operation("nope") is None
    assert spec.timeout_for("mean") == spec.constraints.timeout_s


def test_tool_spec_rejects_duplicate_operations():
    op = make_spec().operations[0]
    with pytest.raises(ValidationError, match="duplicate operation"):
        ToolSpec(tool_id="x", name="x", description="x", operations=(op, op))


@pytest.mark.parametrize("bad", ["Has Spaces", "1starts_with_digit", "UPPER"])
def test_tool_id_must_be_identifier(bad):
    with pytest.raises(ValidationError):
        make_spec(bad)


def test_capability_names_are_validated():
    with pytest.raises(ValidationError, match="invalid capability"):
        OperationSpec(name="op", description="d", capabilities=("Web Search",))


def test_remote_transport_requires_endpoint():
    with pytest.raises(ValidationError, match="requires an endpoint"):
        ToolSpec(
            tool_id="remote",
            name="r",
            description="r",
            transport="http",
            operations=make_spec().operations,
        )


def test_tool_spec_is_immutable():
    spec = make_spec()
    with pytest.raises(ValidationError):
        spec.reliability = 1.0


def test_access_level_ordering():
    assert AccessLevel.WRITE.covers(AccessLevel.READ)
    assert not AccessLevel.READ.covers(AccessLevel.WRITE)
    assert AccessLevel.ADMIN.covers(AccessLevel.EXECUTE)


# --- ExecutionPlan ---------------------------------------------------------------------


def _task(tid, deps=(), **kw):
    return TaskSpec(task_id=tid, capability="data.compute", depends_on=deps, **kw)


def test_layers_group_independent_tasks():
    plan = ExecutionPlan(
        request_id="r",
        tasks=(_task("a"), _task("b"), _task("c", ("a", "b")), _task("d", ("c",))),
    )
    assert plan.layers() == [["a", "b"], ["c"], ["d"]]


def test_refs_add_implicit_dependencies():
    t = _task("b", inputs={"values": {"$ref": "a.output.values"}, "nested": [{"$ref": "c"}]})
    assert set(t.depends_on) == {"a", "c"}
    plan = ExecutionPlan(request_id="r", tasks=(_task("a"), _task("c"), t))
    assert plan.layers() == [["a", "c"], ["b"]]


def test_cycle_is_rejected():
    with pytest.raises(PlanCycleError):
        ExecutionPlan(request_id="r", tasks=(_task("a", ("b",)), _task("b", ("a",))))


def test_unknown_dependency_is_rejected():
    with pytest.raises(UnknownDependencyError):
        ExecutionPlan(request_id="r", tasks=(_task("a", ("ghost",)),))


def test_self_dependency_is_rejected():
    with pytest.raises(ValidationError, match="depends on itself"):
        _task("a", ("a",))


def test_duplicate_task_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate task_id"):
        ExecutionPlan(request_id="r", tasks=(_task("a"), _task("a")))


def test_tool_binding_must_be_complete():
    with pytest.raises(ValidationError, match="together"):
        _task("a", tool_id="calc")


def test_dependents_are_transitive():
    plan = ExecutionPlan(
        request_id="r",
        tasks=(_task("a"), _task("b", ("a",)), _task("c", ("b",)), _task("d")),
    )
    assert plan.dependents("a") == {"b", "c"}
    assert plan.dependents("d") == set()


# --- Errors & config -------------------------------------------------------------------


def test_error_info_from_orchestrator_error_keeps_recoverable_flag():
    info = ErrorInfo.from_exception(ToolTimeoutError("slow"))
    assert info.code == "tool_timeout" and info.recoverable


def test_error_info_from_unexpected_exception_is_not_recoverable():
    info = ErrorInfo.from_exception(ValueError("boom"))
    assert info.code == "unexpected_error" and not info.recoverable


def test_settings_from_env():
    s = Settings.from_env({"ORCH_MAX_CONCURRENCY": "3", "ORCH_ADMIN_TOKEN": "s3cret", "OTHER": "x"})
    assert s.max_concurrency == 3
    assert "s3cret" not in repr(s)


def test_invalid_settings_raise_configuration_error():
    with pytest.raises(ConfigurationError):
        Settings.from_env({"ORCH_MAX_CONCURRENCY": "0"})
