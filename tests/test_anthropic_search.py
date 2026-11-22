"""AnthropicSearchBackend with a fake client (no network, no cost)."""

from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from orchestrator.config import Settings
from orchestrator.contracts import ExecutionContext, Principal
from orchestrator.core import register_default_tools
from orchestrator.errors import ToolExecutionError, TransientToolError
from orchestrator.registry import ToolRegistry
from orchestrator.routing.anthropic_planner import FALLBACK_BETA
from orchestrator.tools import WebSearchTool
from orchestrator.tools.anthropic_search import SEARCH_TOOL_TYPE, AnthropicSearchBackend


def result(url, title="T", page_age=None):
    return NS(type="web_search_result", url=url, title=title, page_age=page_age, encrypted_content="enc")


def search_block(*results):
    return NS(type="web_search_tool_result", tool_use_id="srv_1", content=list(results))


def error_block(code):
    return NS(type="web_search_tool_result", tool_use_id="srv_1",
              content=NS(type="web_search_tool_result_error", error_code=code))


def cite(url, text):
    return NS(type="web_search_result_location", url=url, cited_text=text, title=None, encrypted_index="x")


def text_block(text, *citations):
    return NS(type="text", text=text, citations=list(citations) or None)


def response(*content, stop_reason="end_turn"):
    return NS(content=list(content), stop_reason=stop_reason)


class FakeMessages:
    def __init__(self, *responses, error=None):
        self.responses, self.error, self.calls = list(responses), error, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.responses.pop(0)


def backend(*responses, error=None, **kw):
    messages = FakeMessages(*responses, error=error)
    client = NS(beta=NS(messages=messages))
    return AnthropicSearchBackend(client, **kw), messages


async def test_results_come_from_structured_blocks_and_citations():
    b, messages = backend(response(
        NS(type="server_tool_use", id="srv_1", name="web_search", input={"query": "revenue benchmarks"}),
        search_block(result("https://a.example/report", "Report A", page_age="2026-09-14"),
                     result("https://b.example/x", "B", page_age="3 days ago")),
        text_block("A says median revenue is 180k.", cite("https://a.example/report", "  The median\nrevenue was 180k.  ")),
        # A citation for a URL that was not a search result must not leak in as a snippet.
        text_block("Also see C.", cite("https://c.example/", "made up")),
    ))
    out = await b.search("revenue benchmarks", 5)
    assert [r.url for r in out] == ["https://a.example/report", "https://b.example/x"]
    assert out[0].snippet == "The median revenue was 180k."
    assert out[0].published_at == "2026-09-14" and out[0].source == "a.example"
    assert out[1].snippet == "" and out[1].published_at is None  # "3 days ago" is not a date

    [call] = messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["tools"] == [{"type": SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 3}]
    assert call["output_config"] == {"effort": "low"}
    assert "<query>\nrevenue benchmarks\n</query>" == call["messages"][0]["content"]


async def test_max_results_and_dedup():
    b, _ = backend(response(search_block(result("https://x/1"), result("https://x/2"), result("https://x/1"),
                                         result("https://x/3"))))
    assert [r.url for r in await b.search("q", 2)] == ["https://x/1", "https://x/2"]


async def test_pause_turn_is_resumed_without_extra_user_message():
    first = response(search_block(result("https://x/1")), stop_reason="pause_turn")
    second = response(search_block(result("https://x/2")))
    b, messages = backend(first, second)
    out = await b.search("q", 5)
    assert [r.url for r in out] == ["https://x/1", "https://x/2"]
    resumed = messages.calls[1]["messages"]
    assert [m["role"] for m in resumed] == ["user", "assistant"]
    assert resumed[1]["content"] is first.content


async def test_pause_turn_loop_is_bounded():
    paused = [response(search_block(result(f"https://x/{i}")), stop_reason="pause_turn") for i in range(10)]
    b, messages = backend(*paused, max_continuations=2)
    out = await b.search("q", 10)
    assert len(messages.calls) == 3 and len(out) == 3


@pytest.mark.parametrize("code,exc", [("too_many_requests", TransientToolError), ("unavailable", TransientToolError),
                                      ("query_too_long", ToolExecutionError), ("invalid_tool_input", ToolExecutionError)])
async def test_search_errors_without_results(code, exc):
    b, _ = backend(response(error_block(code)))
    with pytest.raises(exc) as info:
        await b.search("q", 5)
    assert info.type is exc  # TransientToolError is a subclass; be exact


async def test_partial_errors_with_results_still_return_results():
    b, _ = backend(response(search_block(result("https://x/1")), error_block("max_uses_exceeded")))
    assert len(await b.search("q", 5)) == 1


async def test_refusal_is_not_retried():
    b, _ = backend(response(text_block("no"), stop_reason="refusal"))
    with pytest.raises(ToolExecutionError) as info:
        await b.search("q", 5)
    assert not info.value.recoverable


_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "error,recoverable",
    [
        (anthropic.RateLimitError("x", response=httpx2.Response(429, request=_REQ), body=None), True),
        (anthropic.InternalServerError("x", response=httpx2.Response(500, request=_REQ), body=None), True),
        (anthropic.APIConnectionError(request=_REQ), True),
        (anthropic.AuthenticationError("x", response=httpx2.Response(401, request=_REQ), body=None), False),
        (anthropic.BadRequestError("x", response=httpx2.Response(400, request=_REQ), body=None), False),
    ],
)
async def test_api_errors_map_to_retry_semantics(error, recoverable):
    b, _ = backend(error=error)
    with pytest.raises(ToolExecutionError) as info:
        await b.search("q", 5)
    assert info.value.recoverable is recoverable


def test_domain_filters():
    b, _ = backend(allowed_domains=["sec.gov"])
    assert b._tool["allowed_domains"] == ["sec.gov"]
    with pytest.raises(ValueError):
        backend(allowed_domains=["a.com"], blocked_domains=["b.com"])


async def test_backend_plugs_into_the_web_search_tool():
    b, _ = backend(response(search_block(result("https://a.example/r", "R", page_age="2026-01-02"))))
    tool = WebSearchTool(b)
    reg = ToolRegistry()
    await reg.register(tool)
    ctx = ExecutionContext(request_id="r", execution_id="e", task_id="t", principal=Principal(user_id="u"))
    out = await tool.invoke("search", {"query": "abc"}, ctx)
    reg.validate_output("web_search", "search", out)
    assert out["backend"] == "anthropic_web_search" and out["results"][0]["published_at"] == "2026-01-02"


async def test_startup_registers_web_search_only_when_configured(tmp_path):
    off = ToolRegistry()
    await register_default_tools(off, Settings(file_tool_root=tmp_path / "a"))
    assert "web_search" not in off
    on = ToolRegistry()
    await register_default_tools(on, Settings(file_tool_root=tmp_path / "b", search_backend="anthropic",
                                              llm_api_key="sk-test"))
    assert on.get("web_search").adapter._backend.name == "anthropic_web_search"
