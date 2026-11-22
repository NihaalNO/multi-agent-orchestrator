"""Web Search tool.

The adapter is backend-agnostic: it validates and normalises results from a ``SearchBackend``.
``StaticSearchBackend`` serves fixed results for tests and offline demos. A live backend is an
open decision (see docs/ARCHITECTURE.md §6); adding one means implementing ``SearchBackend``
only — the adapter, schemas and orchestrator stay unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts import (
    AccessLevel,
    ExecutionConstraints,
    ExecutionContext,
    OperationSpec,
    ToolSpec,
)
from orchestrator.tools.base import ToolAdapter


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    snippet: str = ""
    published_at: str | None = Field(default=None, description="ISO date if the source gives one.")
    source: str = ""


@runtime_checkable
class SearchBackend(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]: ...

    async def health_check(self) -> bool: ...


class StaticSearchBackend:
    """Returns canned results whose title or snippet shares a word with the query."""

    name = "static"

    def __init__(self, results: Sequence[SearchResult]) -> None:
        self._results = list(results)

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        words = {w for w in query.lower().split() if len(w) > 2}
        hits = [r for r in self._results if words & set(f"{r.title} {r.snippet}".lower().split())]
        return hits[:max_results]

    async def health_check(self) -> bool:
        return True


_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "snippet": {"type": "string"},
        "published_at": {"type": ["string", "null"]},
        "source": {"type": "string"},
    },
    "required": ["title", "url"],
}


def build_spec(reliability: float = 0.6) -> ToolSpec:
    return ToolSpec(
        tool_id="web_search",
        name="Web Search",
        description="Searches the web and returns titles, URLs and snippets.",
        # Web content is less trustworthy than local data; conflict resolution uses this.
        reliability=reliability,
        constraints=ExecutionConstraints(
            # A Claude-backed search runs several searches and model turns; allow for that.
            timeout_s=90, max_retries=2, max_concurrency=4, cost=1.0, expected_latency_ms=15000
        ),
        operations=(
            OperationSpec(
                name="search",
                description="Run a web search query.",
                capabilities=("web.search",),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 2, "maxLength": 400},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "backend": {"type": "string"},
                        "retrieved_at": {"type": "string"},
                        "results": {"type": "array", "items": _RESULT_SCHEMA},
                    },
                    "required": ["query", "backend", "retrieved_at", "results"],
                },
                access=AccessLevel.READ,
            ),
        ),
    )


class WebSearchTool(ToolAdapter):
    spec = build_spec()

    def __init__(self, backend: SearchBackend) -> None:
        self._backend = backend

    async def op_search(self, args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        max_results = args.get("max_results", 5)
        results = await self._backend.search(args["query"], max_results)
        return {
            "query": args["query"],
            "backend": self._backend.name,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "results": [r.model_dump() for r in list(results)[:max_results]],
        }

    async def health_check(self) -> bool:
        return await self._backend.health_check()
