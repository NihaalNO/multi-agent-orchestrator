"""An unreadable plan from an LLM planner is retried once, with the parse error as feedback."""

import json

import pytest

from orchestrator.contracts import PlanningRequest, Principal
from orchestrator.errors import PlanError, PlanOutputError
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RoutingService
from orchestrator.routing.llm_common import parse_plan_output

from fakes import CalcTool, permissive

USER = Principal(user_id="u", roles=frozenset({"tester"}))
GOOD = json.dumps({"rationale": "", "unsupported": [], "tasks": [
    {"task_id": "add", "description": "", "capability": "data.compute", "tool_id": "", "operation": "",
     "inputs_json": "{\"values\": [1, 2]}", "depends_on": [], "compare_group": "", "code": ""}]})
BAD = GOOD.replace('[1, 2]}', '[1, 2]')


class ScriptedPlanner:
    name = "scripted"
    supports_feedback = True

    def __init__(self, outputs, feedback=True):
        self.outputs, self.feedbacks = list(outputs), []
        self.supports_feedback = feedback

    async def plan(self, request, catalog, feedback=None):
        self.feedbacks.append(feedback)
        return parse_plan_output(self.outputs.pop(0), request, self.name)


@pytest.fixture
async def setup():
    reg = ToolRegistry()
    await reg.register(CalcTool())
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    pm = permissive(reg, audit)
    return reg, pm, audit, sink


def test_bad_inputs_json_is_a_plan_output_error():
    with pytest.raises(PlanOutputError, match="inputs_json is not valid JSON"):
        parse_plan_output(BAD, PlanningRequest(text="x"), "p")


async def test_unreadable_plan_is_retried_once_with_the_error(setup):
    reg, pm, audit, sink = setup
    planner = ScriptedPlanner([BAD, GOOD])
    outcome = await RoutingService(reg, planner, pm, audit).plan_and_route(PlanningRequest(text="x"), USER)
    assert outcome.routed.plan is not None
    assert planner.feedbacks[0] is None and "inputs_json is not valid JSON" in planner.feedbacks[1]
    assert sink.events("planner.retry")[0].data["reason"] == "unreadable plan output"


async def test_second_unreadable_plan_is_reported(setup):
    reg, pm, audit, _ = setup
    planner = ScriptedPlanner([BAD, BAD])
    with pytest.raises(PlanError):
        await RoutingService(reg, planner, pm, audit).plan_and_route(PlanningRequest(text="x"), USER)
    assert len(planner.feedbacks) == 2


async def test_planners_without_feedback_are_not_retried(setup):
    reg, pm, audit, _ = setup
    planner = ScriptedPlanner([BAD, GOOD], feedback=False)
    with pytest.raises(PlanOutputError):
        await RoutingService(reg, planner, pm, audit).plan_and_route(PlanningRequest(text="x"), USER)
    assert len(planner.feedbacks) == 1
