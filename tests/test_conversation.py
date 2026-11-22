"""Conversation memory: follow-ups that use earlier results by reference, never across users."""

import pytest

from orchestrator.answers import earlier_evidence
from orchestrator.contracts import InputRef, Principal, TaskSpec
from orchestrator.core import RequestState
from orchestrator.errors import InputResolutionError, InvalidRequestStateError, RequestNotFoundError
from orchestrator.execution.refs import resolve
from orchestrator.routing import RuleBasedPlanner, build_proposal
from orchestrator.routing.llm_common import SYSTEM_PROMPT, user_message
from orchestrator.routing.planner import ToolCatalog

from test_orchestrator import ALICE, EVE, MVP, ROOT, client_for, h, world  # noqa: F401  (world is a fixture)

DOUBLE = [{"task_id": "double", "capability": "data.compute",
           "inputs": {"code": "result = inputs['x'] * 2", "inputs": {"x": {"$ref": "@R1.compute_mean.result.mean"}}}}]


class Planner:
    """Rule-based plans for first messages; ``tasks`` (DOUBLE) for follow-ups. Records follow-ups."""

    name = "capture"

    def __init__(self, tasks=DOUBLE):
        self.tasks, self.requests = tasks, []
        self._rules = RuleBasedPlanner()

    async def plan(self, request, catalog):
        if not request.history and self.tasks is DOUBLE:
            return await self._rules.plan(request, catalog)
        self.requests.append(request)
        return build_proposal(request_id=request.request_id, planner=self.name, tasks=self.tasks)


# --- references -------------------------------------------------------------------------------


def test_earlier_references_parse_and_add_no_dependency():
    ref = InputRef.parse("@R1.compute_mean.result.mean")
    assert (ref.context, ref.task_id, ref.path) == ("R1", "compute_mean", ("result", "mean"))
    assert str(ref) == "@R1.compute_mean.result.mean"
    task = TaskSpec(task_id="double", capability="data.compute", inputs={"x": {"$ref": "@R1.compute_mean.result"}})
    assert task.depends_on == ()
    with pytest.raises(ValueError):
        InputRef.parse("@R1")


def test_earlier_references_resolve_only_from_the_given_context():
    context = {"R1": {"avg": {"result": 129325.0}}}
    assert resolve({"x": {"$ref": "@R1.avg.result"}}, {}, context) == {"x": 129325.0}
    with pytest.raises(InputResolutionError, match="not in this conversation"):
        resolve({"$ref": "@R2.avg.result"}, {}, context)
    with pytest.raises(InputResolutionError, match="no output"):
        resolve({"$ref": "@R1.missing.result"}, {}, context)
    with pytest.raises(InputResolutionError, match="not in this conversation"):
        resolve({"$ref": "@R1.avg.result"}, {"avg": {"result": 1}})  # a plain output is not an earlier result


# --- the flow ---------------------------------------------------------------------------------


async def test_a_follow_up_uses_the_earlier_value_by_reference(world):
    planner = Planner()
    o = world.orchestrator(planner)
    first = await o.submit(MVP, ALICE)
    assert first.state == RequestState.COMPLETED
    follow = await o.submit("now double it", ALICE, context=[first.request_id])
    assert follow.state == RequestState.COMPLETED
    assert follow.context == (first.request_id,)
    assert follow.report.final_output["double"]["result"] == 400.0   # mean 200 from the first request
    [request] = planner.requests
    [turn] = request.history
    assert turn.alias == "R1" and turn.text == MVP
    assert "compute_mean" in turn.steps and "mean: number" in turn.steps["compute_mean"]


async def test_planner_prompt_lists_earlier_steps_by_reference_path(world):
    planner = Planner()
    o = world.orchestrator(planner)
    first = await o.submit(MVP, ALICE)
    await o.submit("now double it", ALICE, context=[first.request_id])
    message = user_message(planner.requests[0], ToolCatalog(entries=()))
    assert "<conversation>\nR1: the user asked: " in message
    line = next(x for x in message.splitlines() if x.strip().startswith("@R1.compute_mean = "))
    assert "{result: {" in line and "mean: number" in line  # shapes, not the data
    assert "never retype values from earlier turns" in SYSTEM_PROMPT


async def test_no_context_means_no_conversation_block(world):
    planner = Planner(tasks=[{"task_id": "c", "capability": "data.compute",
                              "inputs": {"code": "result = 1", "inputs": {}}}])
    await world.orchestrator(planner).submit("compute", ALICE)
    assert planner.requests[0].history == ()
    assert "<conversation>" not in user_message(planner.requests[0], ToolCatalog(entries=()))


@pytest.mark.parametrize("who", [EVE, ROOT])  # another user, and an admin: neither may build on alice's results
async def test_only_your_own_requests_can_be_used(world, who):
    o = world.orchestrator(Planner())
    first = await o.submit(MVP, ALICE)
    with pytest.raises(RequestNotFoundError):
        await o.submit("now double it", who, context=[first.request_id])


async def test_unknown_unfinished_and_too_many_requests_are_refused(world):
    o = world.orchestrator(Planner())
    with pytest.raises(RequestNotFoundError):
        await o.submit("x", ALICE, context=["nope"])
    rejected = await o.submit("book me a flight", ALICE)
    with pytest.raises(InvalidRequestStateError, match="no results to build on"):
        await o.submit("x", ALICE, context=[rejected.request_id])
    with pytest.raises(ValueError, match="at most 5"):
        await o.submit("x", ALICE, context=[f"r{i}" for i in range(6)])


async def test_earlier_values_become_answer_evidence(world):
    o = world.orchestrator(Planner())
    first = await o.submit(MVP, ALICE)
    follow = await o.submit("now double it", ALICE, context=[first.request_id])
    [item] = earlier_evidence(follow.plan, [("R1", first)])
    assert item.kind == "earlier" and item.label == "Earlier result @R1.compute_mean.result.mean"
    assert item.text.endswith("@R1.compute_mean.result.mean = 200.0")


# --- HTTP -------------------------------------------------------------------------------------


async def test_api_context_rules(world):
    with client_for(world) as c:
        first = c.post("/requests", json={"text": MVP}, headers=h("alice")).json()
        # another user's request id looks exactly like a missing one
        r = c.post("/requests", json={"text": "double it", "context": [first["request_id"]]}, headers=h("eve"))
        assert r.status_code == 404
        r = c.post("/requests", json={"text": "x", "context": [f"r{i}" for i in range(6)]}, headers=h("alice"))
        assert r.status_code == 422
        r = c.post("/requests/stream", json={"text": "double it", "context": ["missing"]}, headers=h("alice"))
        assert '"status": 404' in r.text and "event: error" in r.text


def test_chat_sends_recent_history_and_can_start_over():
    from pathlib import Path

    import orchestrator.api.app as app_module

    js = (Path(app_module.__file__).parent / "static" / "chat.js").read_text(encoding="utf-8")
    assert "state.history.slice(-MAX_CONTEXT)" in js and "const MAX_CONTEXT = 5;" in js
    reset = js[js.index("function resetConversation"):js.index("function signOut")]
    assert "state.history = [];" in reset
    assert '$("new-conversation").addEventListener' in js
