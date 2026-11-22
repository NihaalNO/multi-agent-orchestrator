"""Planners: turn a natural-language request into a ``PlanProposal``.

A planner only *proposes*. Everything it returns is re-validated by ``ExecutionPlan`` (structure)
and ``CapabilityRouter`` (tools, capabilities, inputs) before anything runs.

* ``RuleBasedPlanner`` — deterministic keyword rules covering the MVP scenarios. No network,
  no keys; used in tests and as an offline fallback.
* ``AnthropicPlanner`` — Claude with structured outputs. See ``routing/anthropic_planner.py``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator.contracts import (
    ExecutionPlan,
    OperationSpec,
    PlanningRequest,
    PlanProposal,
    TaskSpec,
    ToolSpec,
)
from orchestrator.errors import PlanError
from orchestrator.registry.registry import ToolRegistry


@runtime_checkable
class Planner(Protocol):
    name: str

    async def plan(self, request: PlanningRequest, catalog: "ToolCatalog") -> PlanProposal: ...


# --- Catalog ----------------------------------------------------------------------------------


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: str
    tool_id: str
    operation: str
    description: str
    input_schema: dict[str, Any]
    access: str
    sensitive: bool


class ToolCatalog(BaseModel):
    """What the planner is allowed to know about tools: capabilities, descriptions and input
    schemas of *available* operations. Deliberately excludes runtime state and endpoints."""

    model_config = ConfigDict(frozen=True)

    entries: tuple[CatalogEntry, ...]

    @classmethod
    def from_registry(
        cls,
        registry: ToolRegistry,
        include: Callable[[ToolSpec, OperationSpec], bool] | None = None,
    ) -> "ToolCatalog":
        """``include`` filters operations (e.g. to those the principal could ever use), so the
        planner does not design plans around tools it would be refused."""
        entries = []
        for d in registry.list_tools(include_unavailable=False):
            for op in d.spec.operations:
                if include is not None and not include(d.spec, op):
                    continue
                for cap in op.capabilities:
                    entries.append(
                        CatalogEntry(
                            capability=cap,
                            tool_id=d.spec.tool_id,
                            operation=op.name,
                            description=f"{d.spec.name}: {op.description}",
                            input_schema=op.input_schema,
                            access=op.access.value,
                            sensitive=op.sensitive,
                        )
                    )
        entries.sort(key=lambda e: (e.capability, e.tool_id, e.operation))
        return cls(entries=tuple(entries))

    @property
    def capabilities(self) -> set[str]:
        return {e.capability for e in self.entries}

    def to_prompt_json(self) -> str:
        """Stable (sorted) JSON so identical catalogs produce identical prompts."""
        return json.dumps([e.model_dump() for e in self.entries], sort_keys=True, indent=1)


def build_proposal(
    *,
    request_id: str,
    planner: str,
    tasks: list[dict[str, Any]],
    unsupported: list[str] | tuple[str, ...] = (),
    rationale: str = "",
) -> PlanProposal:
    """Validate raw task dicts into a ``PlanProposal``. Any structural problem (bad ids,
    unknown dependencies, cycles) becomes a ``PlanError`` — never a partially trusted plan."""
    plan = None
    if tasks:
        try:
            plan = ExecutionPlan(
                request_id=request_id,
                tasks=tuple(TaskSpec.model_validate(t) for t in tasks),
                rationale=rationale,
            )
        except ValidationError as exc:
            raise PlanError(
                f"planner {planner!r} produced an invalid plan",
                details={"errors": exc.errors(include_url=False, include_input=False)},
            ) from exc
        except PlanError as exc:
            exc.details.setdefault("planner", planner)
            raise
    return PlanProposal(
        request_id=request_id,
        plan=plan,
        unsupported=tuple(unsupported),
        rationale=rationale,
        planner=planner,
    )


# --- Rule-based planner ---------------------------------------------------------------------

MEAN_CODE = """\
rows = inputs["rows"]
wanted = inputs["column"].lower()
column = next((c for c in (rows[0].keys() if rows else []) if c.lower() == wanted), None)
if column is None:
    raise KeyError(f"column {inputs['column']!r} not found")
values = [float(r[column]) for r in rows if str(r.get(column, "")).strip() != ""]
if not values:
    raise ValueError(f"column {column!r} has no numeric values")
result = {"column": column, "mean": sum(values) / len(values), "count": len(values)}
"""

_FILE_RE = re.compile(r"\b([\w\-./]+\.(csv|txt|md|json))\b", re.IGNORECASE)
_MEAN_RE = re.compile(r"\b(?:average|mean)\s+(?:of\s+)?(?:the\s+)?([A-Za-z_]\w*)", re.IGNORECASE)
_SEARCH_RE = re.compile(
    r"\b(?:search|look\s+up)\s+(?:the\s+web\s+|online\s+)?(?:for\s+)?([^.;!?]+)", re.IGNORECASE
)
_CSV_HINT = re.compile(r"\bcsv\b", re.IGNORECASE)


class RuleBasedPlanner:
    """Deterministic planner for the MVP request shapes:
    read a CSV / text file, compute a column average with Python, and run a web search.
    Requests it does not recognise are returned as ``unsupported`` rather than guessed at."""

    name = "rule_based"

    async def plan(self, request: PlanningRequest, catalog: ToolCatalog) -> PlanProposal:
        text = request.text
        tasks: list[dict[str, Any]] = []
        notes: list[str] = []

        file_path, kind = self._find_file(request)
        mean_match = _MEAN_RE.search(text)
        mean_col = mean_match.group(1) if mean_match else None

        if file_path and (kind == "csv" or mean_col):
            tasks.append({
                "task_id": "load_file",
                "description": f"Load {file_path}",
                "capability": "data.tabular.load",
                "inputs": {"path": file_path},
            })
            notes.append(f"load {file_path}")
        elif file_path:
            tasks.append({
                "task_id": "read_file",
                "description": f"Read {file_path}",
                "capability": "file.read.text",
                "inputs": {"path": file_path},
            })
            notes.append(f"read {file_path}")

        if mean_col:
            if not any(t["task_id"] == "load_file" for t in tasks):
                raise PlanError(
                    "request asks for an average but names no CSV file to read",
                    details={"column": mean_col},
                )
            tasks.append({
                "task_id": "compute_mean",
                "description": f"Average of column {mean_col!r}",
                "capability": "data.compute",
                "inputs": {"code": MEAN_CODE, "inputs": {"rows": {"$ref": "load_file.rows"}, "column": mean_col}},
            })
            notes.append(f"average {mean_col}")

        search = _SEARCH_RE.search(text)
        if search:
            query = search.group(1).strip(" ,")
            tasks.append({
                "task_id": "web_search",
                "description": f"Search the web for {query!r}",
                "capability": "web.search",
                "inputs": {"query": query, "max_results": 5},
            })
            notes.append(f"search {query!r}")

        if not tasks:
            return build_proposal(
                request_id=request.request_id, planner=self.name, tasks=[],
                unsupported=[text], rationale="no rule matched the request",
            )
        return build_proposal(
            request_id=request.request_id, planner=self.name, tasks=tasks,
            rationale="rules matched: " + "; ".join(notes),
        )

    @staticmethod
    def _find_file(request: PlanningRequest) -> tuple[str | None, str | None]:
        m = _FILE_RE.search(request.text)
        if m:
            return m.group(1), m.group(2).lower()
        if _CSV_HINT.search(request.text):
            csvs = [f for f in request.files if f.lower().endswith(".csv")]
            if len(csvs) == 1:
                return csvs[0], "csv"
            if len(csvs) > 1:
                raise PlanError("request mentions a CSV but several were uploaded; name one", details={"files": csvs})
        return None, None
