"""Conflict detection and resolution over an ``ExecutionReport``.

Detection passes (each produces ``Conflict`` records; the input report is not modified):

* **Partial failure** — some tasks did not succeed. Reported so the answer is never presented
  as complete when it is not.
* **Duplicates** — two successful tasks made the identical call (same tool, operation and
  argument digest). Identical outputs are merged; differing outputs are cross-checked.
* **Cross-checks** — tasks sharing a ``compare_group`` answered the same question. Their
  outputs are flattened into fields (``result.mean``) and compared field by field.
* **Merged result lists** — list items carrying a ``url`` (e.g. search results) are merged
  across tasks of the same capability and de-duplicated by normalised URL.
* **Concurrent writes** — two successful non-idempotent tasks wrote the same resource without
  a dependency ordering them, so the final state depends on scheduling.
* **Staleness** — outputs whose own timestamps are older than ``stale_after``.

Resolution policies for a disputed field, applied in ``ResolverConfig.policies`` order:

1. ``normalization`` — values agree once formats are normalised ("200" vs 200.0).
2. ``majority`` — at least three sources and a strict majority agree.
3. ``recency`` — every source has a timestamp and exactly one is newest.
4. ``source_priority`` — one source's registered reliability beats all others by ``reliability_margin``.

If none applies the conflict is **escalated**: the field's value is reported as unknown (``None``)
with every candidate listed. The resolver never invents a value the evidence does not support.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts import (
    Conflict,
    ConflictKind,
    ExecutionPlan,
    ExecutionReport,
    TaskResult,
    TaskStatus,
)
from orchestrator.contracts.results import ConflictCandidate
from orchestrator.observability.audit import AuditLog
from orchestrator.registry.registry import ToolRegistry
from orchestrator.resolution.values import (
    DEFAULT_IGNORE_KEYS,
    flatten,
    normalize,
    normalize_url,
    source_time,
    timestamps_in,
    values_equal,
)

Policy = Literal["normalization", "majority", "recency", "source_priority"]


class ResolverConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    rel_tol: float = Field(default=1e-6, ge=0)
    abs_tol: float = Field(default=1e-9, ge=0)
    reliability_margin: float = Field(default=0.2, ge=0, le=1)
    stale_after: timedelta | None = timedelta(days=365)
    policies: tuple[Policy, ...] = ("normalization", "majority", "recency", "source_priority")
    ignore_keys: frozenset[str] = DEFAULT_IGNORE_KEYS


class _Member(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    result: TaskResult
    fields: dict[str, Any]
    reliability: float | None
    observed_at: datetime | None


class ConflictResolver:
    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditLog | None = None,
        config: ResolverConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._audit = audit or AuditLog()
        self._config = config or ResolverConfig()
        self._now = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ entry point

    def resolve(self, plan: ExecutionPlan, report: ExecutionReport) -> ExecutionReport:
        conflicts: list[Conflict] = []
        resolved: dict[str, Any] = {}
        notes: list[str] = []
        succeeded = [r for r in report.task_results if r.status == TaskStatus.SUCCEEDED]

        self._partial_failures(report, conflicts, notes)
        groups = self._duplicates(succeeded, conflicts)
        for t in plan.tasks:
            if t.compare_group:
                groups.setdefault(t.compare_group, [])
        for t in plan.tasks:
            if t.compare_group:
                groups[t.compare_group].extend(r for r in succeeded if r.task_id == t.task_id)
        expected = {g: sum(1 for t in plan.tasks if t.compare_group == g) for g in groups}
        for name, results in groups.items():
            self._cross_check(name, results, expected.get(name) or len(results), conflicts, resolved, notes)
        self._merge_url_lists(plan, succeeded, conflicts, resolved)
        self._concurrent_writes(plan, succeeded, conflicts, notes)
        self._staleness(succeeded, conflicts, notes)

        for c in conflicts:
            self._audit.record(
                "conflict.escalated" if c.escalated else ("conflict.resolved" if c.resolved else "conflict.detected"),
                request_id=report.request_id, execution_id=report.execution_id,
                conflict_kind=c.kind.value, tasks=list(c.task_ids), field=c.field, policy=c.policy,
            )
        return report.model_copy(update={
            "conflicts": (*report.conflicts, *conflicts),
            "resolved": {**report.resolved, **resolved},
            "uncertainty": (*report.uncertainty, *notes),
        })

    # ------------------------------------------------------------------ passes

    def _partial_failures(self, report: ExecutionReport, conflicts: list[Conflict], notes: list[str]) -> None:
        missing = [r for r in report.task_results if r.status != TaskStatus.SUCCEEDED]
        if not missing:
            return
        parts = [f"{r.task_id} ({r.status.value}{': ' + r.error.message if r.error else ''})" for r in missing]
        total = len(report.task_results)
        conflicts.append(Conflict(
            kind=ConflictKind.PARTIAL_FAILURE,
            task_ids=tuple(r.task_id for r in missing),
            description=f"{len(missing)} of {total} task(s) did not complete: " + "; ".join(parts),
            candidates=tuple(ConflictCandidate(task_id=r.task_id, tool_id=r.tool_id) for r in missing),
        ))
        notes.append(f"The result is incomplete: {len(missing)} of {total} task(s) did not complete ("
                     + ", ".join(r.task_id for r in missing) + ").")

    def _duplicates(self, succeeded: list[TaskResult], conflicts: list[Conflict]) -> dict[str, list[TaskResult]]:
        """Identical calls: merge identical outputs; return differing ones as cross-check groups."""
        by_call: dict[tuple[str | None, str | None, str], list[TaskResult]] = defaultdict(list)
        for r in succeeded:
            if r.args_digest:
                by_call[(r.tool_id, r.operation, r.args_digest)].append(r)
        groups: dict[str, list[TaskResult]] = {}
        for (tool_id, op, _), results in by_call.items():
            if len(results) < 2:
                continue
            ids = tuple(r.task_id for r in results)
            flat = [flatten(r.output, self._config.ignore_keys) for r in results]
            if all(self._same(flat[0], f) for f in flat[1:]):
                conflicts.append(Conflict(
                    kind=ConflictKind.DUPLICATE, task_ids=ids,
                    description=f"{len(ids)} tasks made the identical call {tool_id}.{op} and got the same output",
                    resolved=True, policy="identical_results", chosen_task_id=ids[0],
                    resolution=f"kept the output of {ids[0]}",
                ))
            else:
                conflicts.append(Conflict(
                    kind=ConflictKind.DUPLICATE, task_ids=ids,
                    description=f"{len(ids)} identical calls to {tool_id}.{op} returned different outputs; cross-checking",
                ))
                groups[f"duplicate_{ids[0]}"] = list(results)
        return groups

    def _cross_check(self, group: str, results: list[TaskResult], expected: int,
                     conflicts: list[Conflict], resolved: dict[str, Any], notes: list[str]) -> None:
        unique = list({r.task_id: r for r in results}.values())
        if len(unique) < 2:
            notes.append(f"Could not cross-check {group!r}: only {len(unique)} of {expected} result(s) available.")
            return
        members = [
            _Member(result=r, fields=flatten(r.output, self._config.ignore_keys),
                    reliability=self._reliability(r.tool_id), observed_at=source_time(r.output))
            for r in unique
        ]
        paths = sorted({p for m in members for p in m.fields})
        shared = [p for p in paths if sum(p in m.fields for m in members) >= 2]
        if not shared:
            notes.append(f"Could not cross-check {group!r}: the outputs have no fields in common.")
            return

        values: dict[str, Any] = {}
        for path in shared:
            have = [m for m in members if path in m.fields]
            if all(self._eq(have[0].fields[path], m.fields[path]) for m in have[1:]):
                values[path] = have[0].fields[path]
                continue
            conflict = self._resolve_field(group, path, have)
            conflicts.append(conflict)
            values[path] = conflict.value if conflict.resolved else None
            if conflict.resolved and conflict.kind == ConflictKind.CONTRADICTION:
                notes.append(f"{group}.{path}: sources disagreed; accepted {conflict.value!r} from "
                             f"{conflict.chosen_task_id} by {conflict.policy}.")
            elif not conflict.resolved:
                notes.append(f"{group}.{path}: sources disagree and the evidence is insufficient to choose; "
                             "value left unresolved for review.")
        resolved[group] = values

    def _resolve_field(self, group: str, path: str, members: list[_Member]) -> Conflict:
        cfg = self._config
        candidates = tuple(
            ConflictCandidate(task_id=m.result.task_id, tool_id=m.result.tool_id, value=m.fields[path],
                              reliability=m.reliability, observed_at=m.observed_at)
            for m in members
        )
        ids = tuple(m.result.task_id for m in members)
        base = dict(task_ids=ids, field=path, candidates=candidates)

        # Cluster by normalised value.
        clusters: list[list[_Member]] = []
        for m in members:
            for c in clusters:
                if self._eq(normalize(c[0].fields[path]), normalize(m.fields[path])):
                    c.append(m)
                    break
            else:
                clusters.append([m])
        clusters.sort(key=lambda c: -len(c))

        def accept(kind: ConflictKind, policy: str, winner: _Member, why: str) -> Conflict:
            value = normalize(winner.fields[path]) if kind == ConflictKind.FORMAT_MISMATCH else winner.fields[path]
            return Conflict(kind=kind, description=f"{group}.{path}: {why}", resolved=True, policy=policy,
                            chosen_task_id=winner.result.task_id, value=value,
                            resolution=f"accepted {value!r} from {winner.result.task_id}", **base)

        for policy in cfg.policies:
            if policy == "normalization" and len(clusters) == 1:
                return accept(ConflictKind.FORMAT_MISMATCH, policy, members[0],
                              "values differ only in format")
            if policy == "majority" and len(members) >= 3 and len(clusters[0]) * 2 > len(members):
                return accept(ConflictKind.CONTRADICTION, policy, clusters[0][0],
                              f"{len(clusters[0])} of {len(members)} sources agree")
            if policy == "recency" and all(m.observed_at for m in members):
                newest = sorted(members, key=lambda m: m.observed_at, reverse=True)  # type: ignore[arg-type,return-value]
                if newest[0].observed_at != newest[1].observed_at:
                    return accept(ConflictKind.CONTRADICTION, policy, newest[0],
                                  f"newest source is from {newest[0].observed_at.isoformat()}")  # type: ignore[union-attr]
            if policy == "source_priority" and all(m.reliability is not None for m in members):
                ranked = sorted(members, key=lambda m: m.reliability, reverse=True)  # type: ignore[arg-type,return-value]
                best, runner_up = ranked[0], ranked[1]
                if best.reliability - runner_up.reliability >= cfg.reliability_margin:  # type: ignore[operator]
                    return accept(ConflictKind.CONTRADICTION, policy, best,
                                  f"{best.result.tool_id} is the most reliable source "
                                  f"({best.reliability} vs {runner_up.reliability})")

        return Conflict(
            kind=ConflictKind.CONTRADICTION,
            description=f"{group}.{path}: {len(clusters)} different values and no policy could choose between them",
            escalated=True, **base,
        )

    def _merge_url_lists(self, plan: ExecutionPlan, succeeded: list[TaskResult],
                         conflicts: list[Conflict], resolved: dict[str, Any]) -> None:
        by_capability: dict[str, list[TaskResult]] = defaultdict(list)
        capability = {t.task_id: t.capability for t in plan.tasks}
        for r in succeeded:
            if isinstance(r.output, dict) and any(self._url_items(v) for v in r.output.values()):
                by_capability[capability.get(r.task_id, "unknown")].append(r)

        for cap, results in by_capability.items():
            merged: dict[str, dict[str, Any]] = {}
            dup_sources: set[str] = set()
            for r in sorted(results, key=lambda r: -(self._reliability(r.tool_id) or 0)):
                for items in r.output.values():  # type: ignore[union-attr]
                    for item in self._url_items(items):
                        key = normalize_url(item["url"])
                        if key in merged:
                            if r.task_id not in merged[key]["found_by"]:
                                merged[key]["found_by"].append(r.task_id)
                            dup_sources.update(merged[key]["found_by"])
                        else:
                            merged[key] = {**item, "found_by": [r.task_id]}
            resolved[f"merged:{cap}"] = list(merged.values())
            if dup_sources:
                ids = tuple(sorted(dup_sources))
                conflicts.append(Conflict(
                    kind=ConflictKind.DUPLICATE, task_ids=ids,
                    description=f"duplicate {cap} results (same URL) were merged",
                    resolved=True, policy="merged_by_url",
                    resolution=f"{len(merged)} unique item(s) kept; duplicates list every task that found them",
                ))

    def _concurrent_writes(self, plan: ExecutionPlan, succeeded: list[TaskResult],
                           conflicts: list[Conflict], notes: list[str]) -> None:
        by_resource: dict[str, list[TaskResult]] = defaultdict(list)
        for r in succeeded:
            if r.resource and r.tool_id in self._registry and r.operation:
                op = self._registry.get(r.tool_id).spec.operation(r.operation)
                if op is not None and not op.idempotent:
                    by_resource[r.resource].append(r)
        for resource, writes in by_resource.items():
            if len(writes) < 2:
                continue
            ids = tuple(r.task_id for r in writes)
            unordered = [
                (a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
                if a not in plan.dependents(b) and b not in plan.dependents(a)
            ]
            if not unordered:
                conflicts.append(Conflict(
                    kind=ConflictKind.CONCURRENT_WRITE, task_ids=ids,
                    description=f"{len(ids)} writes to {resource}", resolved=True, policy="dependency_order",
                    resolution="the plan's dependencies fix the order of the writes",
                ))
                continue
            conflicts.append(Conflict(
                kind=ConflictKind.CONCURRENT_WRITE, task_ids=ids, escalated=True,
                description=f"{len(ids)} unordered writes to {resource}: the final state depends on which ran last "
                            f"(unordered pairs: {', '.join(f'{a}/{b}' for a, b in unordered)})",
            ))
            notes.append(f"{resource} was written by several independent tasks; its final state needs review.")

    def _staleness(self, succeeded: list[TaskResult], conflicts: list[Conflict], notes: list[str]) -> None:
        if self._config.stale_after is None:
            return
        cutoff = self._now() - self._config.stale_after
        for r in succeeded:
            old = [(p, ts) for p, ts in timestamps_in(r.output) if ts < cutoff]
            if not old:
                continue
            oldest = min(ts for _, ts in old)
            conflicts.append(Conflict(
                kind=ConflictKind.STALE, task_ids=(r.task_id,),
                description=f"{len(old)} item(s) in {r.task_id} are older than {self._config.stale_after.days} days "
                            f"(oldest {oldest.date().isoformat()})",
                field=old[0][0] if len(old) == 1 else None,
            ))
            notes.append(f"Some information from {r.task_id} may be outdated (oldest from {oldest.date().isoformat()}).")

    # ------------------------------------------------------------------ helpers

    def _eq(self, a: Any, b: Any) -> bool:
        return values_equal(a, b, rel_tol=self._config.rel_tol, abs_tol=self._config.abs_tol)

    def _same(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        return a.keys() == b.keys() and all(self._eq(a[k], b[k]) for k in a)

    def _reliability(self, tool_id: str | None) -> float | None:
        if tool_id and tool_id in self._registry:
            return self._registry.get(tool_id).spec.reliability
        return None

    @staticmethod
    def _url_items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list) and value and all(isinstance(i, dict) and isinstance(i.get("url"), str) for i in value):
            return value
        return []
