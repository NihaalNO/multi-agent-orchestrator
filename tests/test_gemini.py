"""Gemini planner and Google Search backend with a fake client (no network, no cost)."""

import json
from types import SimpleNamespace as NS

import pytest
from google.genai import errors

from orchestrator.__main__ import check_config
from orchestrator.config import Settings
from orchestrator.contracts import PlanningRequest
from orchestrator.core import register_default_tools
from orchestrator.errors import (
    ConfigurationError,
    NoMatchingToolError,
    PlanError,
    PlannerUnavailableError,
    ToolExecutionError,
    TransientToolError,
)
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RoutingService, ToolCatalog, build_planner
from orchestrator.routing.gemini_planner import DEFAULT_GEMINI_MODEL, GeminiPlanner
from orchestrator.routing.llm_common import PLAN_SCHEMA, SYSTEM_PROMPT
from orchestrator.tools import FileProcessingTool, PythonExecTool, StaticSearchBackend, WebSearchTool
from orchestrator.tools.gemini_search import GeminiSearchBackend

from fakes import TESTER, permissive

MVP = ("Read the uploaded CSV file sales.csv, calculate its average revenue using Python, "
       "and independently search for the latest industry revenue benchmarks.")


class FakeInteractions:
    def __init__(self, *responses, error=None):
        self.responses, self.error, self.calls = list(responses), error, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.responses.pop(0)


def fake_client(*responses, error=None):
    interactions = FakeInteractions(*responses, error=error)
    return NS(aio=NS(interactions=interactions)), interactions


def interaction(output_text=None, status="completed", steps=None, errs=None):
    return NS(id="int_1", status=status, output_text=output_text, steps=steps or [], errors=errs)


def llm_task(tid, cap, inputs, deps=(), tool_id="", operation="", group=""):
    return {"task_id": tid, "description": tid, "capability": cap, "tool_id": tool_id, "operation": operation,
            "inputs_json": json.dumps(inputs), "depends_on": list(deps), "compare_group": group}


PLAN = {
    "rationale": "load, compute, search",
    "unsupported": [],
    "tasks": [
        llm_task("load", "data.tabular.load", {"path": "sales.csv"}),
        llm_task("mean", "data.compute", {"code": "result = 1", "inputs": {"rows": {"$ref": "load.rows"}}},
                 deps=["load"], tool_id="python_exec", operation="run"),
        llm_task("search", "web.search", {"query": "revenue benchmarks"}),
    ],
}


@pytest.fixture
async def registry(tmp_path):
    (tmp_path / "sales.csv").write_text("month,Revenue\njan,100\n", encoding="utf-8")
    reg = ToolRegistry()
    await reg.register(FileProcessingTool(tmp_path))
    await reg.register(PythonExecTool())
    await reg.register(WebSearchTool(StaticSearchBackend([])))
    return reg


# --- Planner ------------------------------------------------------------------------------------------


async def test_gemini_planner_request_shape_and_routing(registry):
    client, calls = fake_client(interaction(json.dumps(PLAN)))
    service = RoutingService(registry, GeminiPlanner(client), permissive(registry))
    outcome = await service.plan_and_route(PlanningRequest(request_id="r", text=MVP, files=("sales.csv",)), TESTER)

    [call] = calls.calls
    assert call["model"] == DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert call["store"] is False
    assert call["system_instruction"] == SYSTEM_PROMPT  # same prompt as the Claude planner
    assert call["response_format"] == {"type": "text", "mime_type": "application/json", "schema": PLAN_SCHEMA}
    assert "<user_request>" in call["input"] and "- sales.csv" in call["input"]

    assert outcome.proposal.planner == "gemini" and outcome.routed.complete
    assert outcome.routed.plan.layers() == [["load", "search"], ["mean"]]


async def test_gemini_plans_get_the_same_untrusted_handling(registry):
    plan = {**PLAN, "tasks": [llm_task("s", "web.search", {"query": "abc"}, tool_id="admin_shell", operation="run")]}
    client, _ = fake_client(interaction(json.dumps(plan)))
    outcome = await RoutingService(registry, GeminiPlanner(client), permissive(registry)).plan_and_route(
        PlanningRequest(text="x"), TESTER)
    assert outcome.routed.decisions[0].tool_id == "web_search"
    assert "not registered" in outcome.routed.decisions[0].proposal_rejected


async def test_gemini_unsupported_request(registry):
    client, _ = fake_client(interaction(json.dumps({"rationale": "", "tasks": [], "unsupported": ["send email"]})))
    with pytest.raises(NoMatchingToolError):
        await RoutingService(registry, GeminiPlanner(client), permissive(registry)).plan_and_route(
            PlanningRequest(text="email"), TESTER)


@pytest.mark.parametrize(
    "resp,match",
    [
        (interaction(status="failed", errs=[NS(code="INTERNAL", message="boom")]), "ended failed.*boom"),
        (interaction(json.dumps(PLAN), status="incomplete"), "cut off"),
        (interaction("not json"), "did not match"),
        (interaction(None), "no text output"),
    ],
)
async def test_gemini_bad_outputs(registry, resp, match):
    client, _ = fake_client(resp)
    with pytest.raises(PlanError, match=match):
        await GeminiPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))


def _err(cls, code, status, message="msg"):
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


@pytest.mark.parametrize(
    "error,expected",
    [
        (_err(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), PlannerUnavailableError),
        (_err(errors.ServerError, 503, "UNAVAILABLE"), PlannerUnavailableError),
        (_err(errors.ClientError, 403, "PERMISSION_DENIED"), ConfigurationError),
        (_err(errors.ClientError, 400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
         ConfigurationError),
        (_err(errors.ClientError, 400, "INVALID_ARGUMENT", "schema too deep"), PlanError),
        (TimeoutError(), PlannerUnavailableError),
    ],
)
async def test_gemini_errors_are_mapped(registry, error, expected):
    client, _ = fake_client(error=error)
    with pytest.raises(expected) as info:
        await GeminiPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert info.type is expected
    assert info.value.recoverable == (expected is PlannerUnavailableError)


async def test_unexpected_exceptions_are_not_disguised(registry):
    client, _ = fake_client(error=RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        await GeminiPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))


async def test_no_api_call_without_tools():
    client, calls = fake_client()
    p = await GeminiPlanner(client).plan(PlanningRequest(text="x"), ToolCatalog(entries=()))
    assert p.plan is None and calls.calls == []


# --- Search backend -----------------------------------------------------------------------------------


def cite(url, start, end, title=None):
    return NS(type="url_citation", url=url, title=title, start_index=start, end_index=end)


def output_step(text, *annotations):
    return NS(type="model_output", content=[NS(type="text", text=text, annotations=list(annotations))], error=None)


ANSWER = "Median SaaS revenue grew 18% in 2026. Retail margins held at 4%."


async def test_search_results_come_from_citation_annotations():
    s1, e1 = 0, ANSWER.index(".") + 1
    steps = [
        NS(type="google_search_call", id="c1", arguments=NS(queries=["revenue benchmarks"])),
        NS(type="google_search_result", call_id="c1", result=[NS(search_suggestions="<div>…</div>")], is_error=False),
        output_step(ANSWER,
                    cite("https://a.example/report", s1, e1, title="a.example"),
                    cite("https://b.example/retail", e1 + 1, len(ANSWER)),
                    cite("https://a.example/report", 0, 5)),  # duplicate URL ignored
    ]
    client, calls = fake_client(interaction(steps=steps))
    out = await GeminiSearchBackend(client).search("revenue benchmarks", 5)
    assert [r.url for r in out] == ["https://a.example/report", "https://b.example/retail"]
    assert out[0].snippet == "Median SaaS revenue grew 18% in 2026." and out[0].title == "a.example"
    assert out[1].title == "b.example" and out[1].published_at is None  # no dates from grounding

    [call] = calls.calls
    assert call["tools"] == [{"type": "google_search"}] and call["store"] is False
    assert call["input"] == "<query>\nrevenue benchmarks\n</query>"


async def test_search_without_citations_returns_nothing():
    client, _ = fake_client(interaction(steps=[output_step("I could not find anything.")]))
    assert await GeminiSearchBackend(client).search("q", 5) == []


async def test_search_error_step_without_results_is_transient():
    steps = [NS(type="google_search_result", call_id="c1", result=[], is_error=True), output_step("…")]
    client, _ = fake_client(interaction(steps=steps))
    with pytest.raises(TransientToolError):
        await GeminiSearchBackend(client).search("q", 5)


@pytest.mark.parametrize(
    "error,recoverable",
    [(_err(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), True), (_err(errors.ServerError, 500, "INTERNAL"), True),
     (_err(errors.ClientError, 403, "PERMISSION_DENIED"), False), (_err(errors.ClientError, 400, "INVALID_ARGUMENT"), False)],
)
async def test_search_api_errors_map_to_retry_semantics(error, recoverable):
    client, _ = fake_client(error=error)
    with pytest.raises(ToolExecutionError) as info:
        await GeminiSearchBackend(client).search("q", 5)
    assert info.value.recoverable is recoverable


async def test_failed_search_interaction():
    client, _ = fake_client(interaction(status="failed"))
    with pytest.raises(ToolExecutionError, match="ended failed"):
        await GeminiSearchBackend(client).search("q", 5)


# --- Configuration -------------------------------------------------------------------------------------


def test_provider_default_models():
    assert Settings().planner_model == "claude-opus-5-5"
    assert Settings(llm_provider="gemini").planner_model == "gemini-3.8-flash"
    assert Settings(llm_provider="gemini", llm_model="gemini-4-carbon").planner_model == "gemini-4-carbon"
    assert Settings(search_backend="gemini").effective_search_model == "gemini-3.8-flash"
    assert Settings().effective_search_model is None


def test_api_key_only_applies_to_the_planner_provider():
    s = Settings(llm_provider="gemini", llm_api_key="g-key", search_backend="anthropic")
    assert s.api_key_for("gemini") == "g-key" and s.api_key_for("anthropic") is None


def test_build_gemini_planner(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert isinstance(build_planner(Settings(llm_provider="gemini", llm_api_key="k")), GeminiPlanner)
    with pytest.raises(ConfigurationError, match="no Gemini API key"):
        build_planner(Settings(llm_provider="gemini"))


def test_check_config_gemini_without_key_is_an_error():
    errs, _ = check_config(Settings(llm_provider="gemini", search_backend="gemini"), environ={})
    assert any("planner and search backend use Gemini but no key" in e for e in errs)
    errs, _ = check_config(Settings(llm_provider="gemini", search_backend="gemini"), environ={"GEMINI_API_KEY": "k"})
    assert not errs


def test_check_config_unknown_provider():
    errs, _ = check_config(Settings(llm_provider="mistral"), environ={})
    assert any("unknown ORCH_LLM_PROVIDER" in e for e in errs)


async def test_startup_registers_gemini_search(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    reg = ToolRegistry()
    await register_default_tools(reg, Settings(file_tool_root=tmp_path, search_backend="gemini"))
    assert reg.get("web_search").adapter._backend.name == "gemini_google_search"
