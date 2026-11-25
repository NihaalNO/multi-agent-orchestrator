"""Replay: re-run a finished request's stored plan and compare every step with the original."""


import pytest

from orchestrator.contracts import AccessLevel, PermissionGrant
from orchestrator.core import Orchestrator, RequestState
from orchestrator.config import Settings
from orchestrator.errors import InvalidRequestStateError, RequestNotFoundError
from orchestrator.permissions import GrantPolicy
from orchestrator.receipts import ReceiptIssuer
from orchestrator.routing import RuleBasedPlanner

from test_orchestrator import (  # noqa: F401  (world is a fixture)
    ALICE, BOSS, DELETE_FLOW, EVE, FULL_GRANTS, MVP, ROOT, ScriptedPlanner, client_for, h, world,
)


def orch(world, planner=None, receipts=False):
    issuer = ReceiptIssuer(policy_grants=lambda: world.permissions.policy.grants) if receipts else None
    return Orchestrator(world.registry, planner or RuleBasedPlanner(), world.permissions, world.approvals,
                        world.audit, settings=Settings(llm_provider="rule_based"), receipts=issuer)


async def test_unchanged_data_replays_identically(world):
    o = orch(world)
    record = await o.submit(MVP, ALICE)
    report = await o.replay(record.request_id, ALICE)
    assert report.verdict == "identical"
    assert report.counts == {"identical": len(record.plan.tasks), "differs": 0, "failed": 0, "not_rerun": 0}
    assert all(s.original_sha256 and s.replay_sha256 for s in report.steps)
    assert report.receipt_matches_record is None and report.policy_changed is None  # no receipt here


async def test_replay_really_runs_instead_of_returning_cached_results(world):
    o = orch(world)
    record = await o.submit(MVP, ALICE)
    started = len(world.sink.events("task.started"))
    await o.replay(record.request_id, ALICE)
    assert len(world.sink.events("task.started")) - started == len(record.plan.tasks)
    assert not world.sink.events("task.deduplicated")


async def test_changed_data_is_reported_with_paths(world, tmp_path):
    o = orch(world)
    record = await o.submit(MVP, ALICE)
    root = world.registry.get("file_processing").adapter.root
    (root / "sales.csv").write_text("month,Revenue\njan,100\nfeb,200\nmar,600\n", encoding="utf-8")
    report = await o.replay(record.request_id, ALICE)
    assert report.verdict == "differs"
    by_task = {s.task_id: s for s in report.steps}
    mean = by_task["compute_mean"]
    assert mean.outcome == "differs" and any("mean" in p for p in mean.changed)
    assert mean.original_sha256 != mean.replay_sha256
    assert by_task["load_file"].outcome == "differs"


async def test_steps_with_side_effects_are_never_repeated(world):
    o = orch(world, ScriptedPlanner(DELETE_FLOW))
    record = await o.submit("delete record 1", ALICE)
    await world.approvals.decide(record.pending_approvals["delete"], approver=BOSS, approve=True)
    done = await o.resume(record.request_id, ALICE)
    assert done.state == RequestState.COMPLETED
    deletes = []
    original_delete = world.records.op_delete

    async def counting_delete(args, ctx):
        deletes.append(args)
        return await original_delete(args, ctx)

    world.records.op_delete = counting_delete
    approvals_before = len(world.approvals.list(None))
    report = await o.replay(done.request_id, ALICE)
    by_task = {s.task_id: s for s in report.steps}
    assert by_task["delete"].outcome == "not_rerun" and "side effects" in by_task["delete"].note
    assert by_task["read"].replay_status is not None  # the read step did run again
    assert deletes == []                                # the delete did not
    assert len(world.approvals.list(None)) == approvals_before  # and no new approval was requested


async def test_policy_change_and_record_tampering_are_reported(world):
    o = orch(world, receipts=True)
    record = await o.submit(MVP, ALICE)
    world.permissions._policy = GrantPolicy([*FULL_GRANTS, PermissionGrant(subject="role:extra", tool_id="calc")])
    report = await o.replay(record.request_id, ALICE)
    assert report.policy_changed is True and report.receipt_matches_record is True
    world.permissions._policy = GrantPolicy(FULL_GRANTS)
    # Tamper with the stored record after the fact: the replay says the receipt no longer matches.
    results = list(record.report.task_results)
    results[0] = results[0].model_copy(update={"output": {"tampered": True}})
    o.store.put(record.model_copy(update={"report": record.report.model_copy(update={"task_results": tuple(results)})}))
    again = await o.replay(record.request_id, ALICE)
    assert again.receipt_matches_record is False and again.policy_changed is False


async def test_lost_permissions_make_the_replay_incomplete(world):
    o = orch(world)
    record = await o.submit(MVP, ALICE)
    world.permissions._policy = GrantPolicy([g for g in FULL_GRANTS if g.tool_id != "python_exec"])
    report = await o.replay(record.request_id, ALICE)
    world.permissions._policy = GrantPolicy(FULL_GRANTS)
    assert report.verdict == "incomplete"
    mean = next(s for s in report.steps if s.task_id == "compute_mean")
    assert mean.outcome == "failed" and mean.replay_status == "denied"


async def test_replay_leaves_the_original_record_untouched(world):
    o = orch(world)
    record = await o.submit(MVP, ALICE)
    before = o.store.get(record.request_id).model_dump_json()
    await o.replay(record.request_id, ALICE)
    assert o.store.get(record.request_id).model_dump_json() == before
    assert world.sink.events("request.replayed")[0].data["verdict"] == "identical"


@pytest.mark.parametrize("who", [EVE, ROOT])  # another user, and an admin (may view, may not act)
async def test_only_the_requester_can_replay(world, who):
    o = orch(world)
    record = await o.submit(MVP, ALICE)
    with pytest.raises(RequestNotFoundError):
        await o.replay(record.request_id, who)


async def test_requests_that_did_not_run_cannot_be_replayed(world):
    o = orch(world)
    rejected = await o.submit("book me a flight", ALICE)
    with pytest.raises(InvalidRequestStateError):
        await o.replay(rejected.request_id, ALICE)
    waiting = await orch(world, ScriptedPlanner(DELETE_FLOW)).submit("delete record 1", ALICE)
    assert waiting.state == RequestState.AWAITING_APPROVAL


async def test_api_replay(world):
    with client_for(world) as c:
        rid = c.post("/requests", json={"text": MVP}, headers=h("alice")).json()["request_id"]
        r = c.post(f"/requests/{rid}/replay", headers=h("alice"))
        assert r.status_code == 200 and r.json()["verdict"] == "identical"
        assert c.post(f"/requests/{rid}/replay", headers=h("eve")).status_code == 404
        rejected = c.post("/requests", json={"text": "book me a flight"}, headers=h("alice")).json()["request_id"]
        assert c.post(f"/requests/{rejected}/replay", headers=h("alice")).status_code == 409


def test_chat_offers_replay_on_finished_replies():
    from pathlib import Path

    import orchestrator.api.app as app_module

    js = (Path(app_module.__file__).parent / "static" / "chat.js").read_text(encoding="utf-8")
    assert "function replayControl" in js and '/replay`' in js
    assert '"not re-run"' in js and "Replayed now" in js
