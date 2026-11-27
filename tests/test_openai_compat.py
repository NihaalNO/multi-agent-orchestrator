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
        (GROQ, "openai/gpt-oss-120b", GROQ.base_url, "json_schema"),
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
    assert call["model"] == "openai/gpt-oss-120b"
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
        (_status(openai.APIStatusError, 413), ConfigurationError),  # e.g. over Groq's tokens-per-minute limit
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
        if profile.default_model:
            assert DEFAULT_MODELS[name] == profile.default_model
        else:  # a generic endpoint has no common model name: ORCH_LLM_MODEL is required
            assert name not in DEFAULT_MODELS


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


async def test_explicit_output_budget_uses_each_providers_parameter(registry):
    # Found in the first live Groq run: without an explicit budget, gpt-oss's hidden reasoning used
    # up Groq's default limit and the plan was cut off ("max completion tokens reached").
    for profile, param in ((GROQ, "max_completion_tokens"), (OLLAMA, "max_tokens")):
        client, calls = fake_client(completion(json.dumps(PLAN)))
        await OpenAICompatPlanner(profile, client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
        call = calls.calls[0]
        assert call[param] == 4096
        other = {"max_completion_tokens", "max_tokens"} - {param}
        assert not other & set(call)



def test_output_budget_is_configurable(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    p = build_planner(Settings(llm_provider="groq", llm_api_key="gsk", llm_max_output_tokens=2048))
    assert p._max_output_tokens == 2048


# --- Dedicated `code` field (found in live runs: code inside inputs_json came back over-escaped or
# crammed onto one line) -------------------------------------------------------------------------

from orchestrator.routing.llm_common import parse_plan_output  # noqa: E402

MULTILINE = "total = 0\nfor r in inputs['rows']:\n    total += float(r['Revenue'])\nresult = total / len(inputs['rows'])"


def _code_task(code, inputs):
    return {"task_id": "avg", "description": "avg", "capability": "data.compute", "tool_id": "python_exec",
            "operation": "run", "inputs_json": json.dumps(inputs), "depends_on": [], "compare_group": "",
            "code": code}


def test_code_field_arrives_with_real_line_breaks():
    text = json.dumps({"rationale": "", "unsupported": [], "tasks": [_code_task(MULTILINE, {"inputs": {"rows": []}})]})
    task = parse_plan_output(text, PlanningRequest(text="x"), "groq").plan.tasks[0]
    assert task.inputs == {"inputs": {"rows": []}, "code": MULTILINE}
    compile(task.inputs["code"], "<task>", "exec")  # valid, multi-line Python


def test_code_field_wins_and_old_style_still_works():
    both = _code_task("result = 2", {"code": "result = 1", "inputs": {}})
    old_style = {**_code_task("", {"code": "result = 1", "inputs": {}}), "task_id": "old"}
    text = json.dumps({"rationale": "", "unsupported": [], "tasks": [both, old_style]})
    tasks = parse_plan_output(text, PlanningRequest(text="x"), "groq").plan.tasks
    assert tasks[0].inputs["code"] == "result = 2"
    assert tasks[1].inputs["code"] == "result = 1"


async def test_code_field_end_to_end_through_a_provider(registry):
    plan = {"rationale": "", "unsupported": [], "tasks": [_code_task(MULTILINE, {"inputs": {"rows": []}})]}
    client, calls = fake_client(completion(json.dumps(plan)))
    outcome = await RoutingService(registry, OpenAICompatPlanner(GROQ, client), permissive(registry)).plan_and_route(
        PlanningRequest(text="x"), TESTER)
    assert outcome.routed.complete and outcome.routed.plan.task("avg").inputs["code"] == MULTILINE
    schema = calls.calls[0]["response_format"]["json_schema"]["schema"]["properties"]["tasks"]["items"]
    assert "code" in schema["required"]



class FlakyCompletions:
    """Fails with Groq's output-validation 400 a given number of times, then succeeds."""

    def __init__(self, failures, response):
        self.failures, self.response, self.calls = failures, response, 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise openai.BadRequestError(
                "Error code: 400 - Failed to validate JSON. Please adjust your prompt.",
                response=httpx2.Response(400, request=_REQ), body=None)
        return self.response


@pytest.mark.parametrize("failures,ok", [(1, True), (2, False)])
async def test_output_validation_failure_is_retried_once(registry, failures, ok):
    completions = FlakyCompletions(failures, completion(json.dumps(PLAN)))
    planner = OpenAICompatPlanner(GROQ, NS(chat=NS(completions=completions)))
    if ok:
        proposal = await planner.plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
        assert proposal.plan is not None
    else:
        with pytest.raises(PlanError, match="Failed to validate JSON"):
            await planner.plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert completions.calls == min(failures + 1, 2)


async def test_other_bad_requests_are_not_retried(registry):
    client, calls = fake_client(error=_status(openai.BadRequestError, 400))
    with pytest.raises(PlanError):
        await OpenAICompatPlanner(GROQ, client).plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert len(calls.calls) == 1



# --- OpenAI-compatible endpoints ----------------------------------------------------------------

OPENAI = PROVIDERS["openai"]


@pytest.mark.parametrize("url,mode,param", [
    ("https://api.openai.com/v1", "json_schema", "max_completion_tokens"),     # OpenAI itself
    ("http://localhost:8000/v1", "json_object", "max_tokens"),                 # vLLM / LM Studio
    ("https://openrouter.ai/api/v1", "json_object", "max_tokens"),             # a hosted gateway
])
def test_openai_compatible_modes_follow_the_base_url(url, mode, param):
    assert OPENAI.json_mode("any-model", url) == mode
    assert OPENAI.tokens_param(url) == param


def test_openai_compatible_base_url_and_key_resolution():
    env = {"OPENAI_BASE_URL": "http://localhost:1234/v1"}
    assert OPENAI.resolve_base_url(None, {}) == "https://api.openai.com/v1"
    assert OPENAI.resolve_base_url(None, env) == "http://localhost:1234/v1"            # SDK convention
    assert OPENAI.resolve_base_url("https://gw.example/v1", env) == "https://gw.example/v1"  # ORCH_LLM_BASE_URL wins
    assert OPENAI.key_needed("https://api.openai.com/v1") and OPENAI.key_needed("https://gw.example/v1")
    for local in ("http://localhost:8000/v1", "http://127.0.0.1:1234/v1", "http://[::1]:8000/v1"):
        assert not OPENAI.key_needed(local)
    assert OPENAI.api_key(None, {"OPENAI_API_KEY": "sk-test"}) == "sk-test"
    assert GROQ.resolve_base_url(None, env) == GROQ.base_url  # other providers ignore OPENAI_BASE_URL


def test_build_openai_compatible_planner(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    planner = build_planner(Settings(llm_provider="openai", llm_model="gpt-test"))
    assert isinstance(planner, OpenAICompatPlanner) and planner.name == "openai" and planner.json_mode == "json_schema"
    with pytest.raises(ConfigurationError, match="ORCH_LLM_MODEL"):
        build_planner(Settings(llm_provider="openai"))
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        build_planner(Settings(llm_provider="openai", llm_model="gpt-test"))
    local = build_planner(Settings(llm_provider="openai", llm_model="qwen3", llm_base_url="http://localhost:8000/v1"))
    assert local.json_mode == "json_object"  # no key needed locally, JSON mode for an unknown server


async def test_openai_compatible_request_shape(registry, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)  # the machine's own setting must not leak in
    native, calls = fake_client(completion(json.dumps(PLAN)))
    await OpenAICompatPlanner(OPENAI, native, model="gpt-test").plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    [call] = calls.calls
    assert call["response_format"]["type"] == "json_schema" and call["response_format"]["json_schema"]["strict"] is True
    assert call["max_completion_tokens"] == 4096 and "max_tokens" not in call

    local, calls = fake_client(completion(json.dumps(PLAN)))
    await OpenAICompatPlanner(OPENAI, local, model="qwen3", base_url="http://localhost:8000/v1").plan(
        PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    [call] = calls.calls
    assert call["response_format"] == {"type": "json_object"} and call["max_tokens"] == 4096
    assert json.dumps(PLAN_SCHEMA) in call["messages"][0]["content"]  # the schema goes into the instructions

    forced, calls = fake_client(completion(json.dumps(PLAN)))
    await OpenAICompatPlanner(OPENAI, forced, model="qwen3", base_url="http://localhost:8000/v1",
                              json_mode="json_schema").plan(PlanningRequest(text="x"), ToolCatalog.from_registry(registry))
    assert calls.calls[0]["response_format"]["type"] == "json_schema"  # ORCH_LLM_JSON_MODE override


def test_openai_compatible_composer_uses_the_endpoint_tokens_param(monkeypatch):
    from orchestrator.answers import build_composer

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    native = build_composer(Settings(llm_provider="openai", llm_model="gpt-test"))
    local = build_composer(Settings(llm_provider="openai", llm_model="qwen3", llm_base_url="http://localhost:8000/v1"))
    assert native._tokens_param == "max_completion_tokens" and local._tokens_param == "max_tokens"
    assert build_composer(Settings(llm_provider="openai")) is None  # no model, no composer


def test_check_config_for_openai_compatible():
    errors, _ = check_config(Settings(llm_provider="openai"), environ={})
    assert any("no key is set: set OPENAI_API_KEY" in e for e in errors)
    assert any("no model is set: set ORCH_LLM_MODEL" in e for e in errors)
    errors, warnings = check_config(Settings(llm_provider="openai", llm_model="qwen3",
                                             llm_base_url="http://localhost:8000/v1"), environ={})
    assert not any("openai" in e for e in errors)
    assert any("ORCH_LLM_JSON_MODE=json_schema" in w for w in warnings)
    errors, warnings = check_config(Settings(llm_provider="openai", llm_model="gpt-test"),
                                    environ={"OPENAI_API_KEY": "sk-test"})
    assert not any("openai" in e for e in errors) and not any("JSON mode" in w for w in warnings)



def test_retired_model_is_a_clear_configuration_error():
    from orchestrator.routing.openai_compat_planner import openai_compat_error

    kind, message = openai_compat_error(_status(openai.APIStatusError, 410))
    assert kind == "config" and "no longer available" in message and "ORCH_LLM_MODEL" in message


def test_check_config_warns_about_a_base_url_without_a_path():
    _, warnings = check_config(Settings(llm_provider="openai", llm_model="m", llm_base_url="https://gw.example/"),
                               environ={"OPENAI_API_KEY": "sk-test"})
    assert any("https://gw.example/v1" in w for w in warnings)
    _, warnings = check_config(Settings(llm_provider="openai", llm_model="m", llm_base_url="https://gw.example/v1"),
                               environ={"OPENAI_API_KEY": "sk-test"})
    assert not any("has no path" in w for w in warnings)
