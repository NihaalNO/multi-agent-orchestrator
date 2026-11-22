import json

import pytest

from orchestrator.contracts import AccessLevel, PermissionGrant, PlanningRequest, Principal
from orchestrator.errors import ConfigurationError, PermissionDeniedError
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import GrantPolicy, PermissionManager, load_grants, load_principals
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RoutingService, build_proposal
from orchestrator.routing.planner import RuleBasedPlanner
from orchestrator.tools import FileProcessingTool, PythonExecTool, StaticSearchBackend, WebSearchTool
from orchestrator.tools.base import RESOURCE_PENDING

from fakes import RECORDS_SPEC, RecordsTool, make_spec

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
OPS = {op.name: op for op in RECORDS_SPEC.operations}


def g(**kw):
    kw.setdefault("subject", "role:analyst")
    return PermissionGrant(**kw)


def check(grants, op="read", resource=None, principal=ALICE, pending=False):
    return GrantPolicy(grants).check(principal, RECORDS_SPEC, OPS[op], resource, resource_pending=pending)


# --- GrantPolicy ------------------------------------------------------------------------------------


def test_default_deny():
    d = check([])
    assert not d.allowed and "default deny" in d.reason


def test_role_grant_allows_read():
    d = check([g(tool_id="records", operation="read")])
    assert d.allowed and not d.requires_approval and d.matched_grant.tool_id == "records"


def test_other_roles_and_users_do_not_match():
    assert not check([g(subject="role:admin")]).allowed
    assert check([g(subject="user:alice", operation="read")]).allowed
    assert not check([g(subject="user:bob")]).allowed


def test_unauthorized_write_is_denied():
    # Spec §9 scenario 4: a read-only grant must not permit a write.
    d = check([g(tool_id="records", max_access=AccessLevel.READ)], op="update")
    assert not d.allowed and "write access required" in d.reason


def test_higher_access_level_covers_lower():
    assert check([g(max_access=AccessLevel.WRITE)], op="read").allowed


def test_deny_overrides_allow():
    grants = [g(max_access=AccessLevel.WRITE), g(effect="deny", operation="update")]
    assert check(grants, op="read").allowed
    assert not check(grants, op="update").allowed


def test_resource_scoping():
    grants = [g(resources=("db:records/1", "db:records/1?",))]
    assert check(grants, resource="db:records/1").allowed
    assert check(grants, resource="db:records/12").allowed
    assert not check(grants, resource="db:records/2").allowed
    d = check(grants, resource=None)
    assert not d.allowed and "resource" in d.reason


def test_deny_rule_cannot_be_bypassed_by_case():
    grants = [g(resources=("file:*",)), g(effect="deny", resources=("file:private/*",))]
    assert not check(grants, resource="file:PRIVATE/salaries.csv").allowed
    assert check(grants, resource="file:public/a.csv").allowed


def test_pending_resource_is_provisional():
    d = check([g(resources=("db:records/1",))], pending=True)
    assert d.allowed and d.provisional
    d = check([g(), g(effect="deny", resources=("db:records/9",))], pending=True)
    assert d.allowed and d.provisional  # a deny might apply once the resource is known
    assert not check([g()], pending=True).provisional


def test_sensitive_operation_needs_allow_sensitive_then_approval():
    d = check([g(max_access=AccessLevel.WRITE)], op="delete")
    assert not d.allowed and "sensitive" in d.reason
    d = check([g(max_access=AccessLevel.WRITE, allow_sensitive=True)], op="delete")
    assert d.allowed and d.requires_approval


def test_grant_subject_format_is_validated():
    with pytest.raises(ValueError):
        PermissionGrant(subject="analyst")


# --- Adapter-derived resources -------------------------------------------------------------------


def test_file_resource_is_normalised(tmp_path):
    tool = FileProcessingTool(tmp_path)
    assert tool.resource_for("read_csv", {"path": "reports/../secret.csv"}) == "file:secret.csv"
    assert tool.resource_for("read_csv", {"path": "a\\b.csv"}) == "file:a/b.csv"
    assert tool.resource_for("read_csv", {"path": {"$ref": "x.path"}}) is RESOURCE_PENDING
    assert tool.resource_for("list_files", {}) == "file:"


def test_traversal_cannot_satisfy_a_folder_scoped_grant(tmp_path):
    tool = FileProcessingTool(tmp_path)
    policy = GrantPolicy([g(tool_id="file_processing", resources=("file:reports/*",))])
    op = tool.spec.operation("read_csv")
    res = tool.resource_for("read_csv", {"path": "reports/../secret.csv"})
    assert not policy.check(ALICE, tool.spec, op, res).allowed


# --- PermissionManager + routing ----------------------------------------------------------------


@pytest.fixture
async def registry():
    reg = ToolRegistry()
    await reg.register(RecordsTool())
    return reg


@pytest.fixture
def sink():
    return InMemoryAuditSink()


def manager(registry, grants, sink=None):
    return PermissionManager(GrantPolicy(grants), registry, AuditLog(sink) if sink else None)


async def test_manager_derives_resource_from_args_and_audits(registry, sink):
    pm = manager(registry, [g(resources=("db:records/1",))], sink)
    assert pm.check(ALICE, "records", "read", {"id": 1}, request_id="r", task_id="t").allowed
    assert not pm.check(ALICE, "records", "read", {"id": 2}).allowed
    [first, second] = sink.events("permission.decision")
    assert first.data["resource"] == "db:records/1" and first.data["allowed"] is True
    assert first.request_id == "r" and first.task_id == "t"
    assert second.data["allowed"] is False


async def test_require_raises_when_denied(registry):
    with pytest.raises(PermissionDeniedError):
        manager(registry, []).require(ALICE, "records", "read", {"id": 1})


def _route(pm, registry, *tasks, principal=ALICE):
    from orchestrator.routing import CapabilityRouter

    proposal = build_proposal(request_id="r", planner="t", tasks=list(tasks))
    routed = CapabilityRouter(registry, candidate_filter=pm.candidate_filter(principal)).route(proposal)
    return routed, pm.authorize_plan(principal, routed)


async def test_routing_unauthorized_write_raises_permission_denied(registry):
    pm = manager(registry, [g(tool_id="records", max_access=AccessLevel.READ)])
    with pytest.raises(PermissionDeniedError) as exc:
        _route(pm, registry, {"task_id": "w", "capability": "db.write", "inputs": {"id": 1, "value": "x"}})
    assert exc.value.details["unrouted"][0]["kind"] == "denied"


async def test_router_picks_permitted_alternative(registry):
    from fakes import CalcTool

    await registry.register(CalcTool(make_spec("calc_best", reliability=0.99)))
    await registry.register(CalcTool(make_spec("calc_ok", reliability=0.1)))
    pm = manager(registry, [g(tool_id="calc_ok", max_access=AccessLevel.EXECUTE)])
    routed, auth = _route(pm, registry, {"task_id": "m", "capability": "data.compute", "inputs": {"values": [1]}})
    assert routed.decisions[0].tool_id == "calc_ok" and auth.allowed


async def test_plan_authorization_flags_approvals_and_provisional(registry):
    pm = manager(registry, [g(max_access=AccessLevel.WRITE, allow_sensitive=True, resources=("db:records/*",))])
    routed, auth = _route(
        pm, registry,
        {"task_id": "read", "capability": "db.read", "inputs": {"id": 1}},
        {"task_id": "delete", "capability": "db.delete", "inputs": {"id": {"$ref": "read.id"}}},
    )
    assert auth.allowed
    assert auth.approval_required == ["delete"]
    assert auth.provisional == ["delete"]


async def test_service_hides_unusable_tools_from_planner_and_authorizes(tmp_path):
    (tmp_path / "sales.csv").write_text("Revenue\n1\n", encoding="utf-8")
    reg = ToolRegistry()
    await reg.register(FileProcessingTool(tmp_path))
    await reg.register(PythonExecTool())
    await reg.register(WebSearchTool(StaticSearchBackend([])))
    grants = [g(tool_id="file_processing", resources=("file:*",)), g(tool_id="web_search")]
    pm = manager(reg, grants)

    seen = {}

    class SpyPlanner(RuleBasedPlanner):
        async def plan(self, request, catalog):
            seen["caps"] = catalog.capabilities
            return await super().plan(request, catalog)

    service = RoutingService(reg, SpyPlanner(), pm)
    assert "code.python.execute" not in (await _caps(service, seen))
    # Without a python grant, the average task cannot be routed -> denied, not "no tool".
    with pytest.raises(PermissionDeniedError):
        await service.plan_and_route(PlanningRequest(text="average Revenue in sales.csv"), ALICE)
    outcome = await service.plan_and_route(PlanningRequest(text="read sales.csv"), ALICE)
    assert outcome.authorization.allowed and outcome.routed.complete


async def _caps(service, seen):
    await service.plan_and_route(PlanningRequest(text="search for revenue"), ALICE)
    return seen["caps"]


# --- Loading policy & principals ----------------------------------------------------------------


def test_load_grants_and_principals(tmp_path):
    pol = tmp_path / "p.json"
    pol.write_text(json.dumps({"grants": [{"subject": "role:a", "tool_id": "x", "max_access": "write"}]}))
    [grant] = load_grants(pol)
    assert grant.max_access == AccessLevel.WRITE
    pr = tmp_path / "u.json"
    pr.write_text(json.dumps({"principals": [{"user_id": "alice", "roles": ["a"], "api_key_sha256": "a" * 64}]}))
    assert load_principals(pr)[0].roles == ["a"]


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"grants": [{"subject": "nope"}]}), json.dumps({"grants": [], "extra": 1})],
)
def test_bad_policy_file_is_a_configuration_error(tmp_path, content):
    p = tmp_path / "p.json"
    p.write_text(content)
    with pytest.raises(ConfigurationError):
        load_grants(p)


def test_duplicate_principals_rejected(tmp_path):
    p = tmp_path / "u.json"
    rec = {"user_id": "a", "roles": [], "api_key_sha256": "b" * 64}
    p.write_text(json.dumps({"principals": [rec, {**rec, "user_id": "b"}]}))
    with pytest.raises(ConfigurationError, match="duplicate"):
        load_principals(p)


def test_example_config_files_are_valid():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "config"
    assert load_grants(root / "policy.example.json")
    assert load_principals(root / "principals.example.json")
