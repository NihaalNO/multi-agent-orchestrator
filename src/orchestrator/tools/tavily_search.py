"""Live web search backed by Tavily (https://tavily.com), a search API built for LLM agents.

How results differ from the other backends:

* **Snippets are quotes from the pages** (Tavily's ``content``: the most relevant chunks of each
  page), not text a model wrote. Titles and URLs come from Tavily's index.
* **Publication dates** are requested (``include_published_date``), so staleness checks apply when
  a page has a date.
* **No model runs on our side**: one HTTPS call per search, typically 1–2 seconds. ``basic`` depth
  costs 1 Tavily credit per search; nothing else is billed here.

The key is read from ``TAVILY_API_KEY`` (Tavily's own convention) and sent only as a bearer token to
``api.tavily.com``; it never appears in errors, logs or results.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx2 as httpx  # the HTTP client the provider SDKs already use (locked runtime dependency)

from orchestrator.errors import ConfigurationError, ToolExecutionError, TransientToolError
from orchestrator.tools.web_search import SearchResult

API_URL = "https://api.tavily.com/search"
KEY_ENV = "TAVILY_API_KEY"
_SNIPPET_CHARS = 500
_MAX_RESULTS = 20  # Tavily's own upper bound


def tavily_key(environ: dict[str, str] | None = None) -> str | None:
    value = (os.environ if environ is None else environ).get(KEY_ENV, "").strip()
    return value or None


class TavilySearchBackend:
    name = "tavily"

    def __init__(self, api_key: str | None = None, *, search_depth: str = "basic", timeout_s: float = 30.0,
                 client: httpx.AsyncClient | None = None) -> None:
        key = api_key or tavily_key()
        if not key:
            raise ConfigurationError(f"Tavily web search needs an API key: set {KEY_ENV}")
        self._key = key
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self._depth = search_depth
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        body = {
            "query": query,
            "search_depth": self._depth,
            "topic": "general",
            "max_results": max(1, min(max_results, _MAX_RESULTS)),
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
        }
        try:
            response = await self._client.post(API_URL, json=body, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise TransientToolError("Tavily search timed out") from exc
        except httpx.HTTPError as exc:
            raise TransientToolError(f"could not reach Tavily: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise _error_for(response, self._key)
        try:
            data = response.json()
        except ValueError as exc:
            raise ToolExecutionError("Tavily returned a response that is not JSON") from exc
        return [r for r in (_result(item) for item in data.get("results") or []) if r][:max_results]

    async def health_check(self) -> bool:
        return True  # deliberately no API call: every search costs a credit

    async def aclose(self) -> None:
        await self._client.aclose()


def _result(item: Any) -> SearchResult | None:
    if not isinstance(item, dict) or not item.get("url"):
        return None
    url = str(item["url"])
    host = urlsplit(url).hostname or ""
    snippet = _plain(str(item.get("content") or ""))
    if len(snippet) > _SNIPPET_CHARS:
        snippet = snippet[: _SNIPPET_CHARS - 1] + "…"
    return SearchResult(
        title=str(item.get("title") or host or url),
        url=url,
        snippet=snippet,
        published_at=_iso_date(item.get("published_date")),
        source=host,
    )


_TABLE_RULE = re.compile(r"(?:\|?\s*:?-{3,}:?\s*)+\|?")          # | --- | :---: |, --- ---
_EMPTY_CELLS = re.compile(r"\|(\s*\|)+")                          # | | |
_HEADING = re.compile(r"(^|\s)#{1,6}\s+")                          # ### Title
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|])")                # 12\. → 12.


def _plain(text: str) -> str:
    """Page chunks come as markdown; drop table rules, empty cells, heading marks and backslash
    escapes so the snippet reads as text. Words are never changed."""
    text = _TABLE_RULE.sub(" | ", text)
    text = _EMPTY_CELLS.sub(" | ", text)
    text = _HEADING.sub(r"\1", text)
    text = _ESCAPE.sub(r"\1", text)
    text = " ".join(text.split())
    return text.strip(" |")


def _iso_date(value: Any) -> str | None:
    """Tavily dates are usually RFC 1123 ("Thu, 28 May 2026 11:39:00 GMT"); the result contract and
    the staleness checks use ISO 8601. Unparseable dates are dropped rather than guessed."""
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _error_for(response: httpx.Response, key: str) -> ToolExecutionError:
    status = response.status_code
    # Error text comes from Tavily; scrub the key in case it is ever echoed back.
    detail = _detail(response).replace(key, "[redacted]")
    if status == 401:
        return ToolExecutionError("Tavily rejected the API key (401): check TAVILY_API_KEY")
    if status in (432, 433):
        return ToolExecutionError(f"Tavily usage limit reached ({status}): {detail or 'see app.tavily.com'}")
    if status == 429 or status == 434 or status >= 500:
        retry_after = response.headers.get("retry-after")
        extra = {"retry_after_s": retry_after} if retry_after else None
        return TransientToolError(f"Tavily is busy or unavailable ({status})", details=extra)
    return ToolExecutionError(f"Tavily rejected the search ({status}){': ' + detail if detail else ''}")


def _detail(response: httpx.Response) -> str:
    """A short, key-free description from an error body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("error") or detail.get("message")
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail[:3])
    text = str(detail or (body.get("error") if isinstance(body, dict) else "") or "")
    return text[:200]
