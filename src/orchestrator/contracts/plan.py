"""Execution-plan contracts.

A plan may be produced by an untrusted planner (LLM). Structural validity (unique IDs, known
dependencies, no cycles) is enforced here; tool/permission validity is enforced by the router
and permission manager against the registry, never taken from the plan itself.

Task inputs may reference upstream outputs with ``{"$ref": "<task_id>.<dot.path>"}``. Every
referenced task is automatically added to ``depends_on`` so a reference can never run before
the value it needs exists.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.contracts.tools import CAPABILITY_PATTERN, IDENTIFIER_PATTERN
from orchestrator.errors import PlanCycleError, UnknownDependencyError

REF_KEY = "$ref"


class InputRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    path: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: str) -> "InputRef":
        parts = raw.split(".")
        if not parts or not parts[0]:
            raise ValueError(f"invalid $ref {raw!r}")
        return cls(task_id=parts[0], path=tuple(p for p in parts[1:] if p))


def find_refs(value: Any) -> list[InputRef]:
    """Collect all ``$ref`` markers nested anywhere in an input structure."""
    found: list[InputRef] = []
    if isinstance(value, dict):
        if set(value) == {REF_KEY} and isinstance(value[REF_KEY], str):
            found.append(InputRef.parse(value[REF_KEY]))
        else:
            for v in value.values():
                found.extend(find_refs(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(find_refs(v))
    return found


class TaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str = ""
    capability: str = Field(pattern=CAPABILITY_PATTERN)
    tool_id: str | None = None
    operation: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    resource: str | None = Field(
        default=None, description="Resource the task touches; used for scoping and write locks."
    )

    @model_validator(mode="after")
    def _merge_ref_dependencies(self) -> "TaskSpec":
        ref_deps = [r.task_id for r in find_refs(self.inputs)]
        merged = tuple(dict.fromkeys([*self.depends_on, *ref_deps]))
        if self.task_id in merged:
            raise ValueError(f"task {self.task_id!r} depends on itself")
        if (self.tool_id is None) != (self.operation is None):
            raise ValueError("tool_id and operation must be given together")
        object.__setattr__(self, "depends_on", merged)
        return self

    @property
    def bound(self) -> bool:
        return self.tool_id is not None


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    request_id: str
    tasks: tuple[TaskSpec, ...] = Field(min_length=1)
    rationale: str = ""

    @model_validator(mode="after")
    def _check_structure(self) -> "ExecutionPlan":
        ids = [t.task_id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task_id in plan")
        known = set(ids)
        for t in self.tasks:
            missing = [d for d in t.depends_on if d not in known]
            if missing:
                raise UnknownDependencyError(
                    f"task {t.task_id!r} depends on unknown tasks {missing}",
                    details={"task_id": t.task_id, "missing": missing},
                )
        self.layers()  # raises PlanCycleError on cycles
        return self

    def task(self, task_id: str) -> TaskSpec:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        raise KeyError(task_id)

    def layers(self) -> list[list[str]]:
        """Deterministic topological layering (Kahn). Tasks within a layer are independent."""
        remaining = {t.task_id: set(t.depends_on) for t in self.tasks}
        layers: list[list[str]] = []
        while remaining:
            ready = sorted(tid for tid, deps in remaining.items() if not deps)
            if not ready:
                raise PlanCycleError(
                    "plan contains a dependency cycle",
                    details={"tasks": sorted(remaining)},
                )
            layers.append(ready)
            for tid in ready:
                del remaining[tid]
            for deps in remaining.values():
                deps.difference_update(ready)
        return layers

    def without(self, task_ids: set[str]) -> "ExecutionPlan | None":
        """A copy without the given tasks and everything depending on them (None if empty)."""
        drop = set(task_ids)
        for tid in task_ids:
            drop |= self.dependents(tid)
        kept = tuple(t for t in self.tasks if t.task_id not in drop)
        if not kept:
            return None
        return self.model_copy(update={"tasks": kept})

    def dependents(self, task_id: str) -> set[str]:
        """All tasks that transitively depend on ``task_id``."""
        out: set[str] = set()
        frontier = [task_id]
        while frontier:
            cur = frontier.pop()
            for t in self.tasks:
                if cur in t.depends_on and t.task_id not in out:
                    out.add(t.task_id)
                    frontier.append(t.task_id)
        return out


# --- Planning & routing -------------------------------------------------------------------


class PlanningRequest(BaseModel):
    """What a planner receives. ``text`` is user content and must be treated as untrusted."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    text: str = Field(min_length=1, max_length=20_000)
    files: tuple[str, ...] = Field(default=(), description="Uploaded file paths the request may refer to.")


class PlanProposal(BaseModel):
    """A planner's output. Untrusted until the router has validated and bound it."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    plan: ExecutionPlan | None
    unsupported: tuple[str, ...] = Field(
        default=(), description="Parts of the request the planner found no capability for."
    )
    rationale: str = ""
    planner: str


class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    capability: str
    tool_id: str
    operation: str
    rationale: str
    proposed: str | None = Field(default=None, description="Planner's proposed 'tool.operation', if any.")
    proposal_rejected: str | None = Field(default=None, description="Why the proposal was overridden.")
    alternatives: tuple[str, ...] = ()


class UnroutedTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    capability: str
    reason: str


class RoutedPlan(BaseModel):
    """A plan whose every task is bound to a registered, available tool operation."""

    model_config = ConfigDict(frozen=True)

    plan: ExecutionPlan | None
    decisions: tuple[RoutingDecision, ...] = ()
    unrouted: tuple[UnroutedTask, ...] = ()
    unsupported: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.plan is not None and not self.unrouted and not self.unsupported
