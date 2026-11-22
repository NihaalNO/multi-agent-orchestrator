from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.contracts import ApprovalStatus, Principal
from orchestrator.errors import ApprovalRequiredError, PermissionDeniedError
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import ApprovalManager, ApprovalNotFoundError, args_digest

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
BOB = Principal(user_id="bob", roles=frozenset({"approver"}))
CAROL = Principal(user_id="carol", roles=frozenset({"approver"}))
ARGS = {"id": 5, "password": "hunter2"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sink():
    return InMemoryAuditSink()


@pytest.fixture
def mgr(clock, sink):
    return ApprovalManager(AuditLog(sink), ttl=timedelta(minutes=10), clock=clock)


async def _request(mgr, principal=ALICE, args=ARGS, task_id="t1"):
    return await mgr.request(
        principal=principal, request_id="r1", task_id=task_id, tool_id="records",
        operation="delete", resource="db:records/5", args=args,
    )


async def test_full_flow_approve_then_consume_once(mgr, sink):
    item = await _request(mgr)
    assert item.status == ApprovalStatus.PENDING
    assert "hunter2" not in item.args_preview and "[REDACTED]" in item.args_preview
    assert mgr.list() == [item]

    approved = await mgr.decide(item.approval_id, approver=BOB, approve=True, note="ok")
    assert approved.status == ApprovalStatus.APPROVED and approved.decided_by == "bob"

    consumed = await mgr.consume(item.approval_id, principal=ALICE, args=dict(ARGS))
    assert consumed.status == ApprovalStatus.CONSUMED
    with pytest.raises(ApprovalRequiredError, match="consumed"):
        await mgr.consume(item.approval_id, principal=ALICE, args=ARGS)

    kinds = [e.kind for e in sink.events()]
    assert kinds == ["approval.requested", "approval.approved", "approval.consumed"]


async def test_identical_request_is_deduplicated(mgr):
    a = await _request(mgr)
    assert (await _request(mgr)).approval_id == a.approval_id
    assert (await _request(mgr, args={"id": 6})).approval_id != a.approval_id


async def test_approver_role_required_and_no_self_approval(mgr):
    item = await _request(mgr)
    with pytest.raises(PermissionDeniedError, match="role"):
        await mgr.decide(item.approval_id, approver=ALICE, approve=True)
    own = await _request(mgr, principal=BOB, task_id="t2")
    with pytest.raises(PermissionDeniedError, match="own"):
        await mgr.decide(own.approval_id, approver=BOB, approve=True)
    await mgr.decide(own.approval_id, approver=CAROL, approve=True)


async def test_consume_requires_approval_same_user_and_same_args(mgr):
    item = await _request(mgr)
    with pytest.raises(ApprovalRequiredError, match="pending"):
        await mgr.consume(item.approval_id, principal=ALICE, args=ARGS)
    await mgr.decide(item.approval_id, approver=BOB, approve=True)
    with pytest.raises(PermissionDeniedError, match="different user"):
        await mgr.consume(item.approval_id, principal=CAROL, args=ARGS)
    with pytest.raises(PermissionDeniedError, match="differ"):
        await mgr.consume(item.approval_id, principal=ALICE, args={**ARGS, "id": 6})
    # Key order does not matter: digests are over canonical JSON.
    await mgr.consume(item.approval_id, principal=ALICE, args={"password": "hunter2", "id": 5})


async def test_denied_approval_cannot_be_used_or_redecided(mgr):
    item = await _request(mgr)
    await mgr.decide(item.approval_id, approver=BOB, approve=False)
    with pytest.raises(ApprovalRequiredError, match="denied"):
        await mgr.consume(item.approval_id, principal=ALICE, args=ARGS)
    with pytest.raises(ApprovalRequiredError, match="already"):
        await mgr.decide(item.approval_id, approver=CAROL, approve=True)


async def test_expiry(mgr, clock, sink):
    item = await _request(mgr)
    await mgr.decide(item.approval_id, approver=BOB, approve=True)
    clock.now += timedelta(minutes=11)
    with pytest.raises(ApprovalRequiredError, match="expired"):
        await mgr.consume(item.approval_id, principal=ALICE, args=ARGS)
    assert mgr.get(item.approval_id).status == ApprovalStatus.EXPIRED
    assert sink.events("approval.expired")

    pending = await _request(mgr, task_id="t9")
    clock.now += timedelta(minutes=11)
    assert mgr.get(pending.approval_id).status == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalRequiredError):
        await mgr.decide(pending.approval_id, approver=BOB, approve=True)


async def test_unknown_approval(mgr):
    with pytest.raises(ApprovalNotFoundError):
        mgr.get("nope")


def test_digest_is_canonical():
    assert args_digest({"a": 1, "b": [1, 2]}) == args_digest({"b": [1, 2], "a": 1})
    assert args_digest({"a": 1}) != args_digest({"a": "1"})
