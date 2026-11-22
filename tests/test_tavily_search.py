"""Tavily web search backend, against a fake HTTP transport (no network, no credits)."""

import json

import httpx2 as httpx
import pytest

from orchestrator.__main__ import check_config
from orchestrator.config import Settings
from orchestrator.contracts import ExecutionContext, Principal
from orchestrator.core import register_default_tools
from orchestrator.errors import ConfigurationError, ToolExecutionError, TransientToolError
from orchestrator.registry import ToolRegistry
from orchestrator.tools import WebSearchTool
from orchestrator.tools.tavily_search import API_URL, TavilySearchBackend

KEY = "tvly-test-not-a-real-key"
CTX = ExecutionContext(request_id="r", execution_id="e", task_id="t", principal=Principal(user_id="u"))
RESULTS = {
    "query": "q",
    "results": [
        {"title": "SaaS benchmarks 2026", "url": "https://example.com/report", "content": "Median  ARR\n growth was 26%.",
         "score": 0.91, "published_date": "2026-10-02"},
        {"title": "", "url": "https://news.example.org/a", "content": "x" * 900, "score": 0.5},
        {"title": "no url", "content": "dropped"},
    ],
    "response_time": 1.2,
}


def backend(handler, **kw):
    calls = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return TavilySearchBackend(KEY, client=client, **kw), calls


def respond(status=200, body=RESULTS, headers=None):
    return lambda request: httpx.Response(status, json=body, headers=headers)


async def test_request_shape_and_auth():
    b, calls = backend(respond())
    await b.search("saas revenue benchmarks", 3)
    [req] = calls
    assert str(req.url) == API_URL and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["query"] == "saas revenue benchmarks" and body["max_results"] == 3
    assert body["search_depth"] == "basic" and body["include_published_date"] is True
    assert body["include_answer"] is False and body["include_raw_content"] is False
    assert KEY not in req.content.decode()  # the key travels only in the header


async def test_results_are_page_quotes_with_dates_and_sources():
    b, _ = backend(respond())
    results = await b.search("q", 5)
    assert [r.url for r in results] == ["https://example.com/report", "https://news.example.org/a"]
    first, second = results
    assert first.snippet == "Median ARR growth was 26%."         # whitespace normalised, text unchanged
    assert first.published_at == "2026-10-02T00:00:00+00:00" and first.source == "example.com"
    assert second.title == "news.example.org" and second.published_at is None
    assert len(second.snippet) == 500 and second.snippet.endswith("…")


async def test_max_results_is_respected():
    b, calls = backend(respond())
    assert len(await b.search("q", 1)) == 1
    b, calls = backend(respond())
    await b.search("q", 50)
    assert json.loads(calls[0].content)["max_results"] == 20  # Tavily's upper bound


@pytest.mark.parametrize("status,error,text", [
    (401, ToolExecutionError, "rejected the API key"),
    (432, ToolExecutionError, "usage limit"),
    (433, ToolExecutionError, "usage limit"),
    (400, ToolExecutionError, "rejected the search"),
    (429, TransientToolError, "busy"),
    (500, TransientToolError, "busy"),
])
async def test_errors_are_classified_and_never_leak_the_key(status, error, text):
    # Tavily echoing the key back in an error must not put it into our messages.
    b, _ = backend(respond(status, {"detail": {"error": f"bad request for {KEY}"}}, {"retry-after": "7"}))
    with pytest.raises(error, match=text) as info:
        await b.search("q", 3)
    assert KEY not in str(info.value.message) and KEY not in json.dumps(info.value.details or {})
    assert info.value.recoverable == (error is TransientToolError)


async def test_validation_detail_is_reported():
    b, _ = backend(respond(422, {"detail": [{"msg": "max_results must be <= 20"}]}))
    with pytest.raises(ToolExecutionError, match="max_results must be <= 20"):
        await b.search("q", 3)


async def test_network_failures_are_transient():
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    b, _ = backend(boom)
    with pytest.raises(TransientToolError, match="timed out"):
        await b.search("q", 3)


async def test_non_json_response_is_an_error():
    b, _ = backend(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ToolExecutionError, match="not JSON"):
        await b.search("q", 3)


def test_key_is_required(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="TAVILY_API_KEY"):
        TavilySearchBackend()


async def test_through_the_web_search_tool():
    b, _ = backend(respond())
    out = await WebSearchTool(b).invoke("search", {"query": "saas benchmarks", "max_results": 2}, CTX)
    assert out["backend"] == "tavily" and len(out["results"]) == 2


async def test_registered_by_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", KEY)
    reg = ToolRegistry()
    await register_default_tools(reg, Settings(file_tool_root=tmp_path, search_backend="tavily",
                                               python_sandbox="subprocess"))
    assert "web_search" in reg
    await reg.aclose()


def test_check_config_requires_the_key():
    errors, _ = check_config(Settings(llm_provider="rule_based", search_backend="tavily"), environ={})
    assert any("TAVILY_API_KEY" in e for e in errors)
    errors, _ = check_config(Settings(llm_provider="rule_based", search_backend="tavily"),
                             environ={"TAVILY_API_KEY": KEY})
    assert not any("TAVILY" in e for e in errors)


def test_chat_page_refuses_tavily_keys():
    from pathlib import Path

    import orchestrator.api.app as app_module

    js = (Path(app_module.__file__).parent / "static" / "chat.js").read_text(encoding="utf-8")
    assert '[/^tvly-/, "Tavily"]' in js



@pytest.mark.parametrize("raw,iso", [
    ("Thu, 28 May 2026 11:39:00 GMT", "2026-05-28T11:39:00+00:00"),   # what Tavily sends
    ("2026-10-02T10:00:00Z", "2026-10-02T10:00:00+00:00"),
    ("yesterday", None),                                               # never guessed
])
async def test_dates_become_iso_so_staleness_checks_apply(raw, iso):
    from orchestrator.resolution.values import parse_timestamp

    body = {"results": [{"title": "t", "url": "https://a.example/x", "content": "c", "published_date": raw}]}
    b, _ = backend(respond(200, body))
    [result] = await b.search("q", 1)
    assert result.published_at == iso
    assert (parse_timestamp(result.published_at) is not None) == (iso is not None)


@pytest.mark.parametrize("raw,clean", [
    (r"| | | --- | Metric | Benchmark | | Annual growth | Median 25%", "Metric | Benchmark | Annual growth | Median 25%"),
    (r"| Quarter | Median | --- --- | | Q4/2022 | 41% |", "Quarter | Median | Q4/2022 | 41%"),
    (r"### Growth Rates: 12\. Among SaaS companies", "Growth Rates: 12. Among SaaS companies"),
    ("Revenue grew - slightly - in Q3; a 3-way split", "Revenue grew - slightly - in Q3; a 3-way split"),
])
def test_markdown_noise_is_removed_but_words_are_kept(raw, clean):
    from orchestrator.tools.tavily_search import _plain

    assert _plain(raw) == clean
