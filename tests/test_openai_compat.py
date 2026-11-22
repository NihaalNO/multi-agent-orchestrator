"""Groq and Ollama planners (OpenAI-compatible endpoints) with a fake client: no network, no cost."""

import json
from types import SimpleNamespace as NS

import httpx2
import openai
import pytest

from orchestrator.__main__ import check_config
from orchestrator.config import DEFAULT_MODELS, Settings
from orchestrator.contracts import PlanningRequest
from orchestrator.errors import ConfigurationError, PlanError, PlannerUnavailableError
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RoutingService, ToolCatalog, build_planner
from orchestrator.routing.llm_common import PLAN_SCHEMA, SYSTEM_PROMPT
from orchestrator.routing.openai_compat_planner import PROVIDERS, OpenAICompatPlanner, make_openai_compat_client
from orchestrator.tools import FileProcessingTool, PythonExecTool, StaticSearchBackend, WebSearchTool

from fakes import TESTER, permissive

GROQ, OLLAMA = PROVIDERS["groq"], PROVIDERS["ollama"]


class FakeCompletions:
    def __init__(self, *responses, error=None):
        self.responses, self.error, self.calls = list(responses), error, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.responses.pop(0)


def fake_client(*responses, error=None):
    completions = FakeCompletions(*responses, error=error)
    return NS(chat=NS(completions=completions)), completions


def completion(content, finish_reason="stop", refusal=None):
    return NS(choices=[NS(finish_reason=finish_reason, message=NS(content=content, refusal=refusal))])


def llm_task(tid, cap, inputs, deps=(), tool_id="", operation=""):
    return {"task_id": tid, "description": tid, "capability": cap, "tool_id": tool_id, "operation": operation,
            "inputs_json": json.dumps(inputs), "depends_on": list(deps), "compare_group": ""}


PLAN = {"rationale": "r", "unsupported": [], "tasks": [
    llm_task("load", "data.tabular.load", {"path": "sales.csv"}),
    llm_task("search", "web.search", {"query": "benchmarks"}),
]}


@pytest.fixture
async def registry(tmp_path):
    reg = ToolRegistry()
    await reg.register(FileProcessingTool(tmp_path))
    await reg.register(PythonExecTool())
    await reg.register(WebSearchTool(StaticSearchBackend([])))
    return reg


# --- Structured-output mode per provider and model ---------------------------------------------------


@pytest.mark.parametrize(
    "profile,model,base_url,expected",
    [
        (GROQ, "openai/gpt-oss-20b", GROQ.base_url, "json_schema"),
        (GROQ, "llama-3.3-70b-versatile", GROQ.base_url, "json_object"),  # Llama on Groq: JSON mode only
        (GROQ, "llama-3.1-8b-instant", GROQ.base_url, "json_object"),
        (OLLAMA, "llama3.3", OLLAMA.base_url, "json_schema"),               # local Ollama: any model
        (OLLAMA, "gpt-oss:20b", "https://ollama.com/v1", "json_object"),   # Ollama cloud: no schemas
    ],
)
def test_json_mode_selection(profile, model, base_url, expected):
    assert profile.json_mode(model, base_url) == expected


async def test_groq_strict_schema_request_and_routing(registry):
    client, calls = fake_client(completion(json.dumps(PLAN)))
    planner = OpenAICompatPlanner(GROQ, client)
    outcome = await RoutingService(registry, planner, permissive(registry)).plan_and_route(
        PlanningRequest(text="x", files=("sales.csv",)), TESTER)
    [call] = calls.calls
    assert call["model"] == "openai/gpt-oss-20b"
    assert call["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "execution_plan", "schema": PLAN_SCHEMA, "strict": True}}
    assert call["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}  # shared prompt, unchanged
    assert "<user_request>" in call["messages"][1]["content"]
    assert outcome.proposal.planner == "groq" and outcome.routed.complete


async def test_llama_on_groq_uses_json_mode_with_schema_in_prompt(registry):
    client, calls = fake_client(completion(json.dumps(PLAN)))
    planner = OpenAICompatPlanner(GROQ, client, model="llama-3.3-70b-versatile")
    assert planner.json_mode == "json_object"
    proposal = await planner.plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    [call] = calls.calls
    assert call["response_format"] == {"type": "json_object"}
    system = call["messages"][0]["content"]
    assert system.startswith(SYSTEM_PROMPT) and json.dumps(PLAN_SCHEMA) in system
    assert [t.task_id for t in proposal.plan.tasks] == ["load", "search"]


async def test_ollama_schema_is_not_strict(registry):
    client, calls = fake_client(completion(json.dumps(PLAN)))
    await OpenAICompatPlanner(OLLAMA, client, model="llama3.3").plan(
        PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert calls.calls[0]["response_format"]["json_schema"] == {"name": "execution_plan", "schema": PLAN_SCHEMA}


async def test_json_mode_override(registry):
    client, calls = fake_client(completion(json.dumps(PLAN)))
    await OpenAICompatPlanner(GROQ, client, json_mode="json_object").plan(
        PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert calls.calls[0]["response_format"] == {"type": "json_object"}


async def test_weak_json_mode_output_is_still_validated(registry):
    bad = {"rationale": "r", "tasks": [{"task_id": "a", "capability": "web.search"}], "unsupported": []}
    client, _ = fake_client(completion(json.dumps(bad)))
    with pytest.raises(PlanError, match="did not match the plan schema"):
        await OpenAICompatPlanner(GROQ, client, model="llama-3.3-70b-versatile").plan(
            PlanningRequest(text="x"), ToolCatalog.from_registry(registry))


@pytest.mark.parametrize(
    "resp,match",
    [
        (completion(json.dumps(PLAN), finish_reason="length"), "cut off"),
        (completion(json.dumps(PLAN), finish_reason="content_filter"), "content filter"),
        (completion(None, refusal="no"), "declined"),
        (completion("not json"), "did not match"),
        (NS(choices=[]), "no choices"),
    ],
)
async def test_bad_outputs(registry, resp, match):
    client, _ = fake_client(resp)
    with pytest.raises(PlanError, match=match):
        await OpenAICompatPlanner(GROQ, client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))


_REQ = httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def _status(cls, code):
    return cls("err", response=httpx2.Response(code, request=_REQ), body=None)


@pytest.mark.parametrize(
    "error,expected",
    [
        (_status(openai.RateLimitError, 429), PlannerUnavailableError),
        (_status(openai.InternalServerError, 503), PlannerUnavailableError),
        (openai.APIConnectionError(request=_REQ), PlannerUnavailableError),
        (openai.APITimeoutError(request=_REQ), PlannerUnavailableError),
        (_status(openai.AuthenticationError, 401), ConfigurationError),
        (_status(openai.NotFoundError, 404), ConfigurationError),  # e.g. Ollama model not pulled
        (_status(openai.BadRequestError, 400), PlanError),
    ],
)
async def test_errors_are_mapped(registry, error, expected):
    client, _ = fake_client(error=error)
    with pytest.raises(expected) as info:
        await OpenAICompatPlanner(OLLAMA, client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert info.type is expected
    assert info.value.recoverable == (expected is PlannerUnavailableError)


async def test_no_api_call_without_tools():
    client, calls = fake_client()
    p = await OpenAICompatPlanner(GROQ, client).plan(PlanningRequest(text="x"), ToolCatalog(entries=()))
    assert p.plan is None and calls.calls == []


# --- Clients, keys and configuration ------------------------------------------------------------------


def test_clients_and_keys():
    groq = make_openai_compat_client(GROQ, api_key="gsk-test")
    assert str(groq.base_url).rstrip("/") == "https://api.groq.com/openai/v1"
    with pytest.raises(ConfigurationError, match="no Groq API key"):
        make_openai_compat_client(GROQ, environ={})
    assert GROQ.api_key(None, {"GROQ_API_KEY": "from-env"}) == "from-env"

    local = make_openai_compat_client(OLLAMA, environ={})  # no key needed locally
    assert str(local.base_url).rstrip("/") == "http://localhost:11434/v1"
    assert OLLAMA.api_key(None, {}) == "ollama"
    remote = make_openai_compat_client(OLLAMA, base_url="http://gpu-box:11434/v1", environ={})
    assert str(remote.base_url).rstrip("/") == "http://gpu-box:11434/v1"


def test_default_models_match_provider_profiles():
    for name, profile in PROVIDERS.items():
        assert DEFAULT_MODELS[name] == profile.default_model


def test_build_planner_for_groq_and_ollama(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    p = build_planner(Settings(llm_provider="groq", llm_api_key="gsk"))
    assert isinstance(p, OpenAICompatPlanner) and p.name == "groq" and p.json_mode == "json_schema"
    p = build_planner(Settings(llm_provider="groq", llm_api_key="gsk", llm_model="llama-3.3-70b-versatile"))
    assert p.json_mode == "json_object"
    p = build_planner(Settings(llm_provider="ollama", llm_model="llama3.3", llm_json_mode="json_object"))
    assert p.name == "ollama" and p.json_mode == "json_object"
    with pytest.raises(ConfigurationError, match="no Groq API key"):
        build_planner(Settings(llm_provider="groq"))


def test_check_config():
    errs, _ = check_config(Settings(llm_provider="groq"), environ={})
    assert any("planner is 'groq' but no key" in e for e in errs)
    errs, _ = check_config(Settings(llm_provider="groq"), environ={"GROQ_API_KEY": "k"})
    assert not errs
    errs, warns = check_config(Settings(llm_provider="ollama", llm_model="llama3.3"), environ={})
    assert not errs and any("ollama pull llama3.3" in w for w in warns)
