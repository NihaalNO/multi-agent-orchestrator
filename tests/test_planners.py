import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from orchestrator.config import Settings
from orchestrator.contracts import ExecutionContext, PlanningRequest, Principal
from orchestrator.errors import (
    ConfigurationError,
    NoMatchingToolError,
    PlanCycleError,
    PlanError,
    PlannerUnavailableError,
)
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RoutingService, RuleBasedPlanner, ToolCatalog, build_planner
from orchestrator.routing.anthropic_planner import FALLBACK_BETA, PLAN_SCHEMA, AnthropicPlanner
from fakes import TESTER, permissive
from orchestrator.tools import (
    FileProcessingTool,
    PythonExecTool,
    SearchResult,
    StaticSearchBackend,
    WebSearchTool,
)

MVP_REQUEST = (
    "Read the uploaded CSV file sales.csv, calculate its average revenue using Python, "
    "and independently search for the latest industry revenue benchmarks."
)


@pytest.fixture
async def registry(tmp_path):
    (tmp_path / "sales.csv").write_text("month,Revenue\njan,100\nfeb,200\nmar,300\n", encoding="utf-8")
    reg = ToolRegistry()
    await reg.register(FileProcessingTool(tmp_path))
    await reg.register(PythonExecTool(timeout_s=5))
    await reg.register(WebSearchTool(StaticSearchBackend([
        SearchResult(title="Industry revenue benchmarks", url="https://example.com/bench", snippet="revenue"),
    ])))
    return reg


# --- Catalog -------------------------------------------------------------------------------------


async def test_catalog_lists_only_available_operations(registry):
    await registry.set_enabled("web_search", False)
    catalog = ToolCatalog.from_registry(registry)
    assert "web.search" not in catalog.capabilities
    assert "data.compute" in catalog.capabilities
    assert catalog.to_prompt_json() == ToolCatalog.from_registry(registry).to_prompt_json()


# --- Rule-based planner: MVP end-to-end through routing -------------------------------------------


async def test_rule_based_mvp_plan_routes_to_all_three_tools(registry):
    service = RoutingService(registry, RuleBasedPlanner(), permissive(registry))
    outcome = await service.plan_and_route(PlanningRequest(request_id="r", text=MVP_REQUEST), TESTER)
    proposal, routed = outcome.proposal, outcome.routed
    assert routed.complete
    # File load and web search are independent; the mean waits for the file.
    assert routed.plan.layers() == [["load_file", "web_search"], ["compute_mean"]]
    bindings = {d.task_id: f"{d.tool_id}.{d.operation}" for d in routed.decisions}
    assert bindings == {
        "load_file": "file_processing.read_csv",
        "compute_mean": "python_exec.run",
        "web_search": "web_search.search",
    }
    assert routed.plan.task("web_search").inputs["query"] == "the latest industry revenue benchmarks"


async def test_generated_mean_code_runs_against_real_file_output(registry):
    """Manually performs what the Phase 05 engine will: feed load_file's rows into compute_mean."""
    service = RoutingService(registry, RuleBasedPlanner(), permissive(registry))
    outcome = await service.plan_and_route(PlanningRequest(request_id="r", text=MVP_REQUEST), TESTER)
    _, routed = outcome.proposal, outcome.routed
    ctx = ExecutionContext(request_id="r", execution_id="e", task_id="t", principal=Principal(user_id="u"))
    rows = (await registry.get("file_processing").adapter.invoke("read_csv", {"path": "sales.csv"}, ctx))["rows"]
    mean_task = routed.plan.task("compute_mean")
    args = {**mean_task.inputs, "inputs": {**mean_task.inputs["inputs"], "rows": rows}}
    registry.validate_input("python_exec", "run", args)
    out = await registry.get("python_exec").adapter.invoke("run", args, ctx)
    assert out["result"] == {"column": "Revenue", "mean": 200.0, "count": 3}


async def test_rule_based_uses_single_uploaded_csv():
    p = await RuleBasedPlanner().plan(
        PlanningRequest(text="what is the mean price in the csv", files=("prices.csv",)), ToolCatalog(entries=())
    )
    assert p.plan.task("load_file").inputs == {"path": "prices.csv"}


async def test_rule_based_ambiguous_csv_is_an_error():
    with pytest.raises(PlanError, match="several"):
        await RuleBasedPlanner().plan(
            PlanningRequest(text="average cost from the csv", files=("a.csv", "b.csv")), ToolCatalog(entries=())
        )


async def test_rule_based_unrecognised_request_is_unsupported(registry):
    service = RoutingService(registry, RuleBasedPlanner(), permissive(registry))
    with pytest.raises(NoMatchingToolError) as exc:
        await service.plan_and_route(PlanningRequest(text="book me a flight to Tokyo"), TESTER)
    assert exc.value.details["unsupported"] == ["book me a flight to Tokyo"]


# --- Anthropic planner (fake client; no network) --------------------------------------------------


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(response=None, error=None):
    messages = FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=None, model="claude-opus-5-5",
    )


def llm_task(tid, cap, inputs, deps=(), tool_id="", operation=""):
    return {"task_id": tid, "description": tid, "capability": cap, "tool_id": tool_id,
            "operation": operation, "inputs_json": json.dumps(inputs), "depends_on": list(deps)}


LLM_PLAN = {
    "rationale": "load, compute, search",
    "unsupported": [],
    "tasks": [
        llm_task("load", "data.tabular.load", {"path": "sales.csv"}),
        llm_task("mean", "data.compute",
                 {"code": "result = 1", "inputs": {"rows": {"$ref": "load.rows"}}}, deps=["load"],
                 tool_id="python_exec", operation="run"),
        llm_task("search", "web.search", {"query": "revenue benchmarks"}),
    ],
}


async def test_anthropic_planner_request_shape_and_routing(registry):
    client, messages = fake_client(response(LLM_PLAN))
    service = RoutingService(registry, AnthropicPlanner(client), permissive(registry))
    outcome = await service.plan_and_route(PlanningRequest(request_id="r", text=MVP_REQUEST, files=("sales.csv",)), TESTER)
    proposal, routed = outcome.proposal, outcome.routed

    [call] = messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": PLAN_SCHEMA}}
    user = call["messages"][0]["content"]
    assert "<user_request>" in user and MVP_REQUEST in user and "- sales.csv" in user
    assert "data.tabular.load" in user  # catalog included

    assert proposal.planner == "anthropic"
    assert routed.complete
    assert routed.plan.layers() == [["load", "search"], ["mean"]]
    assert routed.decisions[1].proposed == "python_exec.run" and routed.decisions[1].proposal_rejected is None


async def test_anthropic_planner_reports_unsupported(registry):
    plan = {"rationale": "", "tasks": [], "unsupported": ["send an email to the team"]}
    client, _ = fake_client(response(plan))
    service = RoutingService(registry, AnthropicPlanner(client), permissive(registry))
    with pytest.raises(NoMatchingToolError) as exc:
        await service.plan_and_route(PlanningRequest(text="email the team"), TESTER)
    assert exc.value.details["unsupported"] == ["send an email to the team"]


async def test_anthropic_planner_hallucinated_binding_is_overridden(registry):
    plan = {**LLM_PLAN, "tasks": [llm_task("s", "web.search", {"query": "abc"}, tool_id="admin_shell", operation="run")]}
    client, _ = fake_client(response(plan))
    outcome = await RoutingService(registry, AnthropicPlanner(client), permissive(registry)).plan_and_route(PlanningRequest(text="x"), TESTER)
    _, routed = outcome.proposal, outcome.routed
    assert routed.decisions[0].tool_id == "web_search"
    assert "not registered" in routed.decisions[0].proposal_rejected


@pytest.mark.parametrize(
    "resp,match",
    [
        (response(LLM_PLAN, stop_reason="refusal"), "declined"),
        (response(LLM_PLAN, stop_reason="max_tokens"), "cut off"),
        (response("not json"), "did not match"),
        (response({**LLM_PLAN, "tasks": [{**llm_task("a", "web.search", {}), "inputs_json": "{bad"}]}), "not valid JSON"),
        (response({**LLM_PLAN, "tasks": [{**llm_task("a", "web.search", {}), "inputs_json": "[1]"}]}), "must encode an object"),
    ],
)
async def test_anthropic_planner_bad_outputs(registry, resp, match):
    client, _ = fake_client(resp)
    with pytest.raises(PlanError, match=match):
        await AnthropicPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))


async def test_anthropic_planner_cyclic_plan_rejected(registry):
    plan = {**LLM_PLAN, "tasks": [
        llm_task("a", "web.search", {"query": "abc"}, deps=["b"]),
        llm_task("b", "web.search", {"query": "abc"}, deps=["a"]),
    ]}
    client, _ = fake_client(response(plan))
    with pytest.raises(PlanCycleError):
        await AnthropicPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))


_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status):
    return cls("err", response=httpx2.Response(status, request=_REQ), body=None)


@pytest.mark.parametrize(
    "error,expected",
    [
        (_status_error(anthropic.RateLimitError, 429), PlannerUnavailableError),
        (_status_error(anthropic.InternalServerError, 500), PlannerUnavailableError),
        (anthropic.APIConnectionError(request=_REQ), PlannerUnavailableError),
        (_status_error(anthropic.AuthenticationError, 401), ConfigurationError),
        (_status_error(anthropic.BadRequestError, 400), PlanError),
    ],
)
async def test_anthropic_errors_are_mapped(registry, error, expected):
    client, _ = fake_client(error=error)
    with pytest.raises(expected) as exc:
        await AnthropicPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert exc.value.recoverable == (expected is PlannerUnavailableError)


async def test_anthropic_planner_skips_api_when_no_tools():
    client, messages = fake_client(response(LLM_PLAN))
    p = await AnthropicPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog(entries=()))
    assert p.plan is None and p.unsupported == ("x",) and messages.calls == []


def test_build_planner_from_settings():
    assert isinstance(build_planner(Settings(llm_provider="rule_based")), RuleBasedPlanner)
    planner = build_planner(Settings(llm_api_key="sk-test", llm_effort="high"))
    assert isinstance(planner, AnthropicPlanner)
    with pytest.raises(ConfigurationError):
        build_planner(Settings(llm_provider="openai"))
