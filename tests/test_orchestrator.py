"""Integration tests: the whole lifecycle through the Orchestrator facade and the HTTP API."""

import pytest
from fastapi.testclient import TestClient

from orchestrator.api import create_app
from orchestrator.api.auth import ApiKeyAuthenticator, hash_api_key
from orchestrator.config import Settings
from orchestrator.contracts import AccessLevel, ExecutionStatus, PermissionGrant, Principal, TaskStatus
from orchestrator.core import Orchestrator, RequestState
from orchestrator.errors import (
    ApprovalRequiredError,
    InvalidRequestStateError,
    PlannerUnavailableError,
    RequestNotFoundError,
)
from orchestrator.observability import AuditLog, InMemoryAuditSink, MetricsSink
from orchestrator.permissions import ApprovalManager, GrantPolicy, PermissionManager, PrincipalRecord
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RuleBasedPlanner, build_proposal
from orchestrator.tools import FileProcessingTool, PythonExecTool, SearchResult, StaticSearchBackend, WebSearchTool

from fakes import RecordsTool

MVP = ("Read the uploaded CSV file sales.csv, calculate its average revenue using Python, "
       "and independently search for the latest industry revenue benchmarks.")

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
EVE = Principal(user_id="eve", roles=frozenset({"analyst"}))
BOSS = Principal(user_id="boss", roles=frozenset({"approver"}))
ROOT = Principal(user_id="root", roles=frozenset({"admin"}))


def grant(**kw):
    kw.setdefault("subject", "role:analyst")
    return PermissionGrant(**kw)


FULL_GRANTS = [
    grant(tool_id="file_processing", resources=("file:*",)),
    grant(tool_id="python_exec", max_access=AccessLevel.EXECUTE),
    grant(tool_id="web_search"),
    grant(tool_id="records", max_access=AccessLevel.WRITE, allow_sensitive=True),
]


class CountingRecords(RecordsTool):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def op_read(self, args, ctx):
        self.reads += 1
        return await super().op_read(args, ctx)


class ScriptedPlanner:
    """Returns a fixed plan, or raises. Counts calls."""

    name = "scripted"

    def __init__(self, tasks=None, error=None):
        self.tasks, self.error, self.calls = tasks or [], error, 0

    async def plan(self, request, catalog):
        self.calls += 1
        if self.error:
            raise self.error
        return build_proposal(request_id=request.request_id, planner=self.name, tasks=self.tasks)


DELETE_FLOW = [
    {"task_id": "read", "capability": "db.read", "inputs": {"id": 1}},
    {"task_id": "delete", "capability": "db.delete", "inputs": {"id": {"$ref": "read.id"}}},
]


class World:
    def __init__(self, registry, records, grants=FULL_GRANTS):
        self.registry, self.records = registry, records
        self.sink, self.metrics = InMemoryAuditSink(), MetricsSink()
        self.audit = AuditLog(self.sink, self.metrics)
        self.permissions = PermissionManager(GrantPolicy(grants), registry, self.audit)
        self.approvals = ApprovalManager(self.audit)

    def orchestrator(self, planner=None):
        return Orchestrator(self.registry, planner or RuleBasedPlanner(), self.permissions, self.approvals,
                            self.audit, settings=Settings(llm_provider="rule_based"))


@pytest.fixture
async def world(tmp_path):
    (tmp_path / "sales.csv").write_text("month,Revenue\njan,100\nfeb,200\nmar,300\n", encoding="utf-8")
    reg = ToolRegistry()
    await reg.register(FileProcessingTool(tmp_path))
    await reg.register(PythonExecTool(timeout_s=10))
    await reg.register(WebSearchTool(StaticSearchBackend([
        SearchResult(title="Industry revenue benchmarks", url="https://example.com/bench?utm_source=x",
                     snippet="median revenue 180", published_at="2026-09-01"),
    ])))
    records = CountingRecords()
    await reg.register(records)
    return World(reg, records)


# --- Facade ------------------------------------------------------------------------------------------


async def test_mvp_request_through_the_facade(world):
    record = await world.orchestrator().submit(MVP, ALICE)
    assert record.state == RequestState.COMPLETED and record.error is None
    report = record.report
    assert report.status == ExecutionStatus.SUCCEEDED
    assert report.final_output["compute_mean"]["result"]["mean"] == 200.0
    assert report.resolved["merged:web.search"][0]["url"].startswith("https://example.com/bench")
    assert [s.stage for s in record.stages] == ["plan_route_authorize", "execute", "resolve", "total"]
    assert all(s.duration_ms >= 0 for s in record.stages)
    assert "compute_mean [python_exec.run]: succeeded" in record.summary
    assert record.planner == "rule_based" and record.executions == 1

    kinds = [e.kind for e in world.sink.events()]
    assert kinds[0] == "request.received" and kinds[-1] == "request.finished"
    snap = world.metrics.snapshot()
    assert snap["tools"]["python_exec"]["success_rate"] == 1.0
    assert snap["tools"]["python_exec"]["latency_ms"]["p50"] is not None
    assert snap["requests"]["states"] == {"completed": 1}


async def test_unsupported_request_is_rejected_without_executing(world):
    record = await world.orchestrator().submit("book me a flight to Tokyo", ALICE)
    assert record.state == RequestState.REJECTED and record.error.code == "no_matching_tool"
    assert record.report is None and not world.sink.events("execution.started")
    assert "no_matching_tool" in record.summary


async def test_unauthorized_request_is_rejected(world, tmp_path):
    w = World(world.registry, world.records, grants=[grant(tool_id="file_processing", resources=("file:*",))])
    record = await w.orchestrator().submit("average Revenue in sales.csv", ALICE)
    assert record.state == RequestState.REJECTED and record.error.code == "permission_denied"


async def test_partial_routing_reports_what_was_not_attempted(world):
    w = World(world.registry, world.records,
              grants=[grant(tool_id="file_processing", resources=("file:*",)),
                      grant(tool_id="python_exec", max_access=AccessLevel.EXECUTE)])
    record = await w.orchestrator().submit(MVP, ALICE, allow_partial=True)
    assert record.state == RequestState.COMPLETED
    assert "web_search" not in record.report.final_output
    assert any(note.startswith("Not attempted: web_search") for note in record.report.uncertainty)


async def test_approval_flow_resumes_without_rerunning_finished_tasks(world):
    orch = world.orchestrator(ScriptedPlanner(DELETE_FLOW))
    record = await orch.submit("delete record 1", ALICE)
    assert record.state == RequestState.AWAITING_APPROVAL
    assert set(record.pending_approvals) == {"delete"} and world.records.reads == 1
    assert "Waiting for approval: delete" in record.summary

    with pytest.raises(ApprovalRequiredError, match="pending"):
        await orch.resume(record.request_id, ALICE)

    await world.approvals.decide(record.pending_approvals["delete"], approver=BOSS, approve=True)
    done = await orch.resume(record.request_id, ALICE)
    assert done.state == RequestState.COMPLETED and done.executions == 2
    assert 1 not in world.records.data
    assert world.records.reads == 1  # the read was reused, not repeated
    assert world.sink.events("execution.resumed")[0].data["reused"] == ["read"]
    assert {r.task_id: r.status for r in done.report.task_results} == {
        "read": TaskStatus.SUCCEEDED, "delete": TaskStatus.SUCCEEDED,
    }


async def test_denied_approval_ends_in_partial_completion(world):
    orch = world.orchestrator(ScriptedPlanner(DELETE_FLOW))
    record = await orch.submit("delete record 1", ALICE)
    await world.approvals.decide(record.pending_approvals["delete"], approver=BOSS, approve=False)
    done = await orch.resume(record.request_id, ALICE)
    assert done.state == RequestState.COMPLETED and done.report.status == ExecutionStatus.PARTIAL
    assert 1 in world.records.data


async def test_resume_guards(world):
    orch = world.orchestrator(ScriptedPlanner(DELETE_FLOW))
    record = await orch.submit("delete record 1", ALICE)
    with pytest.raises(RequestNotFoundError):
        await orch.resume(record.request_id, EVE)
    with pytest.raises(RequestNotFoundError):
        await orch.resume(record.request_id, ROOT)  # admins may view but not act
    await world.approvals.decide(record.pending_approvals["delete"], approver=BOSS, approve=True)
    await orch.resume(record.request_id, ALICE)
    with pytest.raises(InvalidRequestStateError):
        await orch.resume(record.request_id, ALICE)


async def test_idempotency_key_returns_the_same_request(world):
    planner = ScriptedPlanner([{"task_id": "read", "capability": "db.read", "inputs": {"id": 2}}])
    orch = world.orchestrator(planner)
    first = await orch.submit("read 2", ALICE, idempotency_key="k-1")
    again = await orch.submit("read 2", ALICE, idempotency_key="k-1")
    other_user = await orch.submit("read 2", EVE, idempotency_key="k-1")
    assert again.request_id == first.request_id and planner.calls == 2
    assert other_user.request_id != first.request_id
    assert world.sink.events("request.duplicate")


async def test_visibility_of_requests(world):
    orch = world.orchestrator()
    record = await orch.submit("book a flight", ALICE)
    assert orch.get(record.request_id, ALICE).request_id == record.request_id
    assert orch.get(record.request_id, ROOT).request_id == record.request_id
    with pytest.raises(RequestNotFoundError):
        orch.get(record.request_id, EVE)
    with pytest.raises(RequestNotFoundError):
        orch.get("nope", ALICE)


async def test_planner_outage_is_a_rejection(world):
    orch = world.orchestrator(ScriptedPlanner(error=PlannerUnavailableError("down")))
    record = await orch.submit("anything", ALICE)
    assert record.state == RequestState.REJECTED and record.error.code == "planner_unavailable"
    assert record.error.recoverable


async def test_unexpected_planner_bug_is_recorded_then_raised(world):
    orch = world.orchestrator(ScriptedPlanner(error=RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        await orch.submit("anything", ALICE)
    [finished] = world.sink.events("request.finished")
    assert finished.data["state"] == "failed"
    assert orch.store.get(finished.request_id).state == RequestState.FAILED


# --- HTTP API -----------------------------------------------------------------------------------------

KEYS = {"alice": "k-alice", "eve": "k-eve", "boss": "k-boss", "root": "k-root"}
ROLES = {"alice": ["analyst"], "eve": ["analyst"], "boss": ["approver"], "root": ["admin"]}


def h(user, **extra):
    return {"Authorization": f"Bearer {KEYS[user]}", **extra}


def client_for(world, planner=None, **settings):
    authn = ApiKeyAuthenticator(
        PrincipalRecord(user_id=u, roles=ROLES[u], api_key_sha256=hash_api_key(k)) for u, k in KEYS.items()
    )
    app = create_app(
        settings=Settings(llm_provider="rule_based", **settings), registry=world.registry, audit=world.audit,
        authenticator=authn, permissions=world.permissions, approvals=world.approvals,
        orchestrator=world.orchestrator(planner),
    )
    return TestClient(app)


async def test_api_mvp_request(world):
    with client_for(world) as c:
        r = c.post("/requests", json={"text": MVP}, headers=h("alice"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "completed" and body["report"]["status"] == "succeeded"
        assert body["report"]["final_output"]["compute_mean"]["result"]["mean"] == 200.0
        rid = body["request_id"]
        assert c.get(f"/requests/{rid}", headers=h("alice")).json()["request_id"] == rid
        assert c.get(f"/requests/{rid}", headers=h("eve")).status_code == 404
        assert c.get(f"/requests/{rid}", headers=h("root")).status_code == 200

        metrics = c.get("/admin/metrics", headers=h("root")).json()
        assert metrics["requests"]["states"]["completed"] == 1
        assert c.get("/admin/metrics", headers=h("alice")).status_code == 403


async def test_api_rejections_use_error_status_codes(world):
    with client_for(world) as c:
        r = c.post("/requests", json={"text": "book me a flight"}, headers=h("alice"))
        assert r.status_code == 422 and r.json()["error"]["code"] == "no_matching_tool"
        assert r.json()["state"] == "rejected"
    with client_for(world, ScriptedPlanner(error=PlannerUnavailableError("down"))) as c:
        assert c.post("/requests", json={"text": "x"}, headers=h("alice")).status_code == 503
        assert c.post("/requests", json={"text": ""}, headers=h("alice")).status_code == 422
        assert c.post("/requests", json={"text": "x"}).status_code == 401


async def test_api_approval_round_trip(world):
    with client_for(world, ScriptedPlanner(DELETE_FLOW)) as c:
        r = c.post("/requests", json={"text": "delete record 1"}, headers=h("alice", **{"Idempotency-Key": "d1"}))
        assert r.status_code == 202
        body = r.json()
        rid, aid = body["request_id"], body["pending_approvals"]["delete"]
        dup = c.post("/requests", json={"text": "delete record 1"}, headers=h("alice", **{"Idempotency-Key": "d1"}))
        assert dup.json()["request_id"] == rid

        assert c.post(f"/requests/{rid}/resume", headers=h("alice")).status_code == 409  # still pending
        assert c.post(f"/approvals/{aid}/approve", headers=h("boss")).status_code == 200
        done = c.post(f"/requests/{rid}/resume", headers=h("alice"))
        assert done.status_code == 200 and done.json()["state"] == "completed"
        assert 1 not in world.records.data
        assert c.post(f"/requests/{rid}/resume", headers=h("alice")).status_code == 409


def test_default_tools_are_registered_on_startup(tmp_path):
    uploads = tmp_path / "uploads"
    authn = ApiKeyAuthenticator([PrincipalRecord(user_id="root", roles=["admin"], api_key_sha256=hash_api_key("k-root"))])
    app = create_app(settings=Settings(llm_provider="rule_based", file_tool_root=uploads), authenticator=authn)
    with TestClient(app) as c:
        tools = {t["spec"]["tool_id"] for t in c.get("/tools", headers=h("root")).json()["tools"]}
    assert tools == {"file_processing", "python_exec"}  # no web search without a real backend
    assert uploads.is_dir()
