"""Durable state: everything that matters must survive a restart (a fresh process on the same file)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.__main__ import check_config
from orchestrator.api import create_app
from orchestrator.api.auth import ApiKeyAuthenticator, hash_api_key
from orchestrator.config import Settings
from orchestrator.contracts import (
    AccessLevel,
    ApprovalStatus,
    ErrorInfo,
    PermissionGrant,
    Principal,
    TaskResult,
    TaskStatus,
)
from orchestrator.core import Orchestrator, RequestRecord, RequestState
from orchestrator.errors import ConfigurationError
from orchestrator.execution import IdempotencyStore
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import ApprovalManager, GrantPolicy, PermissionManager, PrincipalRecord
from orchestrator.registry import ToolRegistry
from orchestrator.routing import build_proposal
from orchestrator.state import SCHEMA_VERSION, StateDB, open_state

from fakes import RecordsTool

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
BOSS = Principal(user_id="boss", roles=frozenset({"approver"}))


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state" / "orchestrator.db"


def restart(path):
    """Simulate a new process: a fresh connection and fresh stores on the same file."""
    return open_state(path)


# --- Requests -----------------------------------------------------------------------------------------


def _record(rid="r1", state=RequestState.COMPLETED, user="alice"):
    now = datetime.now(UTC)
    return RequestRecord(request_id=rid, user_id=user, text="hello", state=state, created_at=now, updated_at=now,
                         summary="done", error=ErrorInfo(code="x", message="y") if state == RequestState.REJECTED else None)


def test_request_records_and_keys_survive_restart(db_path):
    original = _record()
    s = open_state(db_path)
    s.requests.put(original)
    s.requests.bind_key("alice", "idem-1", "r1")
    s.close()

    s2 = restart(db_path)
    assert s2.requests.get("r1") == original  # exact round trip, timestamps included
    assert s2.requests.by_key("alice", "idem-1").request_id == "r1"
    assert s2.requests.by_key("bob", "idem-1") is None
    assert s2.requests.get("missing") is None
    s2.close()


def test_running_requests_are_marked_failed_on_restart_not_rerun(db_path):
    s = open_state(db_path)
    s.requests.put(_record("stuck", RequestState.RUNNING))
    s.requests.put(_record("fine", RequestState.COMPLETED))
    s.close()

    s2 = restart(db_path)
    assert s2.recovered == ["stuck"]
    stuck = s2.requests.get("stuck")
    assert stuck.state == RequestState.FAILED and stuck.error.code == "interrupted_by_restart"
    assert s2.requests.get("fine").state == RequestState.COMPLETED
    s2.close()
    assert restart(db_path).recovered == []  # recovery runs once


# --- Approvals ----------------------------------------------------------------------------------------


async def _ask(mgr, args=None):
    return await mgr.request(principal=ALICE, request_id="r", task_id="t", tool_id="records",
                             operation="delete", resource="db:records/1", args=args or {"id": 1})


async def test_pending_approval_survives_restart_and_can_be_decided_and_consumed(db_path):
    s = open_state(db_path)
    item = await _ask(ApprovalManager(store=s.approvals))
    s.close()

    s2 = restart(db_path)
    mgr = ApprovalManager(store=s2.approvals)
    assert [a.approval_id for a in mgr.list(ApprovalStatus.PENDING)] == [item.approval_id]
    assert (await _ask(mgr)).approval_id == item.approval_id  # dedup works across restarts
    await mgr.decide(item.approval_id, approver=BOSS, approve=True)
    s2.close()

    s3 = restart(db_path)
    mgr = ApprovalManager(store=s3.approvals)
    assert (await mgr.consume(item.approval_id, principal=ALICE, args={"id": 1})).status == ApprovalStatus.CONSUMED
    s3.close()
    assert ApprovalManager(store=restart(db_path).approvals).get(item.approval_id).status == ApprovalStatus.CONSUMED


async def test_approvals_expire_across_restarts(db_path):
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    s = open_state(db_path)
    item = await _ask(ApprovalManager(store=s.approvals, ttl=timedelta(minutes=5), clock=lambda: clock[0]))
    s.close()

    clock[0] += timedelta(minutes=6)
    sink = InMemoryAuditSink()
    mgr = ApprovalManager(AuditLog(sink), store=restart(db_path).approvals, clock=lambda: clock[0])
    new = await _ask(mgr)  # triggers expiry; the expired one is no longer reused
    assert new.approval_id != item.approval_id
    assert mgr.get(item.approval_id).status == ApprovalStatus.EXPIRED
    assert sink.events("approval.expired")


# --- Idempotency --------------------------------------------------------------------------------------


async def test_idempotent_results_survive_restart(db_path):
    calls = []

    async def run():
        calls.append(1)
        return TaskResult(task_id="t", status=TaskStatus.SUCCEEDED, output={"n": 1})

    s = open_state(db_path)
    await IdempotencyStore(backend=s.idempotency).run_once("k", run)
    s.close()

    s2 = restart(db_path)
    again = await IdempotencyStore(backend=s2.idempotency).run_once("k", run)
    assert again.deduplicated and again.output == {"n": 1} and calls == [1]
    s2.close()


async def test_idempotency_expiry_and_failures(db_path):
    now = [1_000_000.0]

    async def ok():
        return TaskResult(task_id="t", status=TaskStatus.SUCCEEDED, output={})

    async def bad():
        return TaskResult(task_id="t", status=TaskStatus.FAILED)

    s = open_state(db_path)
    store = IdempotencyStore(ttl_s=60, clock=lambda: now[0], backend=s.idempotency)
    await store.run_once("ok", ok)
    await store.run_once("bad", bad)
    assert s.idempotency.get("bad", now[0]) is None  # failures are never cached
    now[0] += 61
    assert not (await store.run_once("ok", ok)).deduplicated  # expired -> ran again
    assert s.db.execute("SELECT COUNT(*) FROM idempotency")[0][0] == 1
    s.close()


# --- Schema & config ----------------------------------------------------------------------------------


def test_newer_schema_is_refused(db_path):
    open_state(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(ConfigurationError, match="newer orchestrator"):
        StateDB(db_path)
    errs, _ = check_config(Settings(llm_provider="rule_based", state_path=db_path), environ={})
    assert any("newer orchestrator" in e for e in errs)


def test_check_config_state_path(tmp_path):
    _, warns = check_config(Settings(llm_provider="rule_based"), environ={})
    assert any("ORCH_STATE_PATH is not set" in w for w in warns)
    errs, _ = check_config(Settings(llm_provider="rule_based", state_path=tmp_path), environ={})
    assert any("is a directory" in e for e in errs)


# --- End to end: an approval flow that spans a restart -------------------------------------------------


class Plan:
    name = "fixed"

    async def plan(self, request, catalog):
        return build_proposal(request_id=request.request_id, planner=self.name, tasks=[
            {"task_id": "read", "capability": "db.read", "inputs": {"id": 1}},
            {"task_id": "delete", "capability": "db.delete", "inputs": {"id": {"$ref": "read.id"}}},
        ])


class CountingRecords(RecordsTool):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def op_read(self, args, ctx):
        self.reads += 1
        return await super().op_read(args, ctx)


def _orchestrator(state):
    registry = ToolRegistry()
    records = CountingRecords()
    policy = GrantPolicy([PermissionGrant(subject="role:analyst", tool_id="records", max_access=AccessLevel.WRITE,
                                          allow_sensitive=True)])
    approvals = ApprovalManager(store=state.approvals)
    orch = Orchestrator(registry, Plan(), PermissionManager(policy, registry), approvals,
                        settings=Settings(llm_provider="rule_based"), store=state.requests,
                        idempotency_backend=state.idempotency)
    return orch, registry, records, approvals


async def test_request_awaiting_approval_resumes_after_restart(db_path):
    s = open_state(db_path)
    orch, registry, records, approvals = _orchestrator(s)
    await registry.register(records)
    record = await orch.submit("delete record 1", ALICE)
    assert record.state == RequestState.AWAITING_APPROVAL and records.reads == 1
    approval_id = record.pending_approvals["delete"]
    s.close()

    # --- process restarts: new tool instance, new stores, same database file ---
    s2 = restart(db_path)
    orch, registry, records, approvals = _orchestrator(s2)
    await registry.register(records)
    await approvals.decide(approval_id, approver=BOSS, approve=True)
    done = await orch.resume(record.request_id, ALICE)
    assert done.state == RequestState.COMPLETED and done.executions == 2
    assert 1 not in records.data   # the delete ran in the new process
    assert records.reads == 0      # the read's persisted result was reused, not re-run
    assert {r.task_id: r.status for r in done.report.task_results} == {
        "read": TaskStatus.SUCCEEDED, "delete": TaskStatus.SUCCEEDED}
    assert orch.get(record.request_id, ALICE).state == RequestState.COMPLETED
    s2.close()


def test_api_requests_survive_app_restart(tmp_path):
    db = tmp_path / "state.db"
    authn = ApiKeyAuthenticator([PrincipalRecord(user_id="alice", roles=["analyst"],
                                                 api_key_sha256=hash_api_key("k"))])
    settings = Settings(llm_provider="rule_based", state_path=db, file_tool_root=tmp_path / "uploads")
    h = {"Authorization": "Bearer k", "Idempotency-Key": "first"}
    with TestClient(create_app(settings=settings, authenticator=authn)) as c:
        r = c.post("/requests", json={"text": "book me a flight"}, headers=h)
        rid = r.json()["request_id"]
        assert r.json()["state"] == "rejected"

    with TestClient(create_app(settings=settings, authenticator=authn)) as c:  # a new process
        assert c.get(f"/requests/{rid}", headers=h).json()["state"] == "rejected"
        again = c.post("/requests", json={"text": "book me a flight"}, headers=h)
        assert again.json()["request_id"] == rid  # client idempotency survives the restart
