"""Signed, hash-chained receipts: issuing, offline verification, tamper detection, no data inside."""

import json

import pytest

from orchestrator.__main__ import main as cli
from orchestrator.core import Orchestrator, RequestState
from orchestrator.receipts import (
    GENESIS,
    Receipt,
    ReceiptIssuer,
    Signer,
    generate_key,
    verify_against_record,
    verify_chain,
    verify_signature,
)
from orchestrator.config import Settings
from orchestrator.routing import RuleBasedPlanner

from test_orchestrator import ALICE, BOSS, DELETE_FLOW, MVP, ScriptedPlanner, h, world  # noqa: F401


@pytest.fixture
def signer(tmp_path):
    generate_key(tmp_path / "key.pem")
    return Signer(tmp_path / "key.pem")


def orch(world, signer, planner=None):
    issuer = ReceiptIssuer(signer=signer, policy_grants=lambda: world.permissions.policy.grants,
                           approver=lambda aid: world.approvals.get(aid).decided_by)
    return Orchestrator(world.registry, planner or RuleBasedPlanner(), world.permissions, world.approvals,
                        world.audit, settings=Settings(llm_provider="rule_based"), receipts=issuer)


def ok(findings):
    return [f.message for f in findings if f.level != "ok"] == []


# --- issuing ------------------------------------------------------------------------------------


async def test_a_finished_request_gets_a_signed_receipt_with_hashes_only(world, signer):
    record = await orch(world, signer).submit(MVP, ALICE)
    r = record.receipt
    assert r is not None and r.sequence == 1 and r.previous == GENESIS
    assert r.request_id == record.request_id and r.user_id == "alice" and r.state == "completed"
    assert r.key_id == signer.key_id and r.signature
    assert {s.task_id for s in r.steps} == {t.task_id for t in record.plan.tasks}
    assert all(s.output_sha256.startswith("sha256:") and s.args_sha256.startswith("sha256:") for s in r.steps)
    assert r.policy_sha256.startswith("sha256:") and r.plan_sha256.startswith("sha256:")
    # No data: not the question, not file rows, not the computed mean, not the search result.
    text = json.dumps(r.model_dump(mode="json"))
    for leaked in ("calculate its average", "jan", "200.0", "example.com/bench", "median revenue"):
        assert leaked not in text
    assert world.sink.events("receipt.issued")[0].data["signed"] is True


async def test_rejected_requests_get_a_receipt_too(world, signer):
    record = await orch(world, signer).submit("book me a flight", ALICE)
    assert record.state == RequestState.REJECTED
    assert record.receipt.error == "no_matching_tool" and record.receipt.steps == () and record.receipt.plan_sha256 is None


async def test_receipts_wait_for_approvals_and_name_the_approver(world, signer):
    o = orch(world, signer, ScriptedPlanner(DELETE_FLOW))
    record = await o.submit("delete record 1", ALICE)
    assert record.state == RequestState.AWAITING_APPROVAL and record.receipt is None
    await world.approvals.decide(record.pending_approvals["delete"], approver=BOSS, approve=True)
    done = await o.resume(record.request_id, ALICE)
    step = next(s for s in done.receipt.steps if s.task_id == "delete")
    assert step.approval_id and step.approved_by == "boss" and step.status == "succeeded"


async def test_unsigned_receipts_are_still_chained(world):
    o = orch(world, None)
    first, second = await o.submit(MVP, ALICE), await o.submit(MVP, ALICE)
    assert first.receipt.signature is None and first.receipt.key_id is None
    assert second.receipt.previous == first.receipt.receipt_sha256
    assert verify_signature(first.receipt, "").level == "warning"


# --- verification -------------------------------------------------------------------------------


async def test_signature_verifies_and_detects_tampering(world, signer, tmp_path):
    receipt = (await orch(world, signer).submit(MVP, ALICE)).receipt
    assert verify_signature(receipt, signer.public_pem).level == "ok"
    forged = receipt.model_copy(update={"state": "failed"})
    assert verify_signature(forged, signer.public_pem).level == "error"
    generate_key(tmp_path / "other.pem")
    other = Signer(tmp_path / "other.pem")
    assert "not this public key" in verify_signature(receipt, other.public_pem).message


async def test_receipt_matches_its_record_and_catches_edits(world, signer):
    record = await orch(world, signer).submit(MVP, ALICE)
    public = record.public()
    assert ok(verify_against_record(record.receipt, public))
    edited = json.loads(json.dumps(public, default=str))
    edited["report"]["task_results"][1]["output"]["result"] = {"mean": 999}
    bad = [f.message for f in verify_against_record(record.receipt, edited) if f.level == "error"]
    assert any("compute_mean output: does NOT match" in m for m in bad)


async def test_chain_links_and_detects_gaps_and_rewrites(world, signer):
    o = orch(world, signer)
    receipts = [(await o.submit(MVP, ALICE)).receipt for _ in range(3)]
    assert [r.sequence for r in receipts] == [1, 2, 3]
    assert ok(verify_chain(receipts))
    assert any("gap" in f.message for f in verify_chain([receipts[0], receipts[2]]))
    rewritten = receipts[1].model_copy(update={"user_id": "mallory"})
    assert any("does not link" in f.message for f in verify_chain([receipts[0], rewritten, receipts[2]]))


async def test_follow_ups_record_the_receipts_they_built_on(world, signer):
    from test_conversation import Planner

    o = orch(world, signer, Planner())
    first = await o.submit(MVP, ALICE)
    follow = await o.submit("now double it", ALICE, context=[first.request_id])
    assert follow.receipt.context == {first.request_id: first.receipt.receipt_sha256}


# --- storage, HTTP and CLI ------------------------------------------------------------------------


def test_chain_survives_a_restart(tmp_path, signer):
    from orchestrator.state import open_state

    state = open_state(tmp_path / "s.db")
    issuer = ReceiptIssuer(state.receipts, signer)

    class Rec:  # the minimum a receipt needs
        def __init__(self, rid):
            from datetime import UTC, datetime

            from orchestrator.core import RequestRecord
            self.r = RequestRecord(request_id=rid, user_id="u", text="q", state=RequestState.REJECTED,
                                   created_at=datetime.now(UTC), updated_at=datetime.now(UTC))

    first = issuer.issue(Rec("a").r)
    state.close()
    again = open_state(tmp_path / "s.db")
    second = ReceiptIssuer(again.receipts, signer).issue(Rec("b").r)
    assert second.sequence == 2 and second.previous == first.receipt_sha256
    assert again.receipts.get("a") == first and [r.sequence for r in again.receipts.since(0)] == [1, 2]
    again.close()


async def test_api_receipt_endpoints(world, signer, tmp_path):
    o = orch(world, signer)
    from fastapi.testclient import TestClient

    from orchestrator.api import create_app
    from orchestrator.api.auth import ApiKeyAuthenticator, hash_api_key
    from orchestrator.permissions import PrincipalRecord

    from test_orchestrator import KEYS, ROLES

    authn = ApiKeyAuthenticator(PrincipalRecord(user_id=u, roles=ROLES[u], api_key_sha256=hash_api_key(k))
                                for u, k in KEYS.items())
    app = create_app(settings=Settings(llm_provider="rule_based"), registry=world.registry, audit=world.audit,
                     authenticator=authn, permissions=world.permissions, approvals=world.approvals, orchestrator=o)
    with TestClient(app) as c:
        rid = c.post("/requests", json={"text": MVP}, headers=h("alice")).json()["request_id"]
        receipt = c.get(f"/requests/{rid}/receipt", headers=h("alice")).json()
        assert receipt["request_id"] == rid and receipt["sequence"] == 1
        assert c.get(f"/requests/{rid}/receipt", headers=h("eve")).status_code == 404
        key = c.get("/receipts/public-key").json()  # public: no key needed
        assert key["key_id"] == signer.key_id and key["algorithm"] == "Ed25519"
        chain = c.get("/admin/receipts", headers=h("root")).json()["receipts"]
        assert [r["sequence"] for r in chain] == [1]
        assert c.get("/admin/receipts", headers=h("alice")).status_code == 403
        record = c.get(f"/requests/{rid}", headers=h("alice")).json()

    (tmp_path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (tmp_path / "record.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / "pub.pem").write_text(key["public_key_pem"], encoding="utf-8")
    (tmp_path / "chain.json").write_text(json.dumps({"receipts": chain}), encoding="utf-8")
    assert cli(["verify-receipt", str(tmp_path / "receipt.json"), "--public-key", str(tmp_path / "pub.pem"),
                "--record", str(tmp_path / "record.json")]) == 0
    assert cli(["verify-receipt", "--chain", str(tmp_path / "chain.json"),
                "--public-key", str(tmp_path / "pub.pem")]) == 0
    receipt["state"] = "failed"
    (tmp_path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert cli(["verify-receipt", str(tmp_path / "receipt.json"), "--public-key", str(tmp_path / "pub.pem")]) == 1


def test_receipt_key_command_never_overwrites(tmp_path, capsys):
    assert cli(["receipt-key", "--out", str(tmp_path / "k.pem")]) == 0
    assert "BEGIN PUBLIC KEY" in capsys.readouterr().out
    assert cli(["receipt-key", "--out", str(tmp_path / "k.pem")]) == 1


def test_receipts_are_valid_models():
    assert Receipt.model_json_schema()["properties"]["signature"]
