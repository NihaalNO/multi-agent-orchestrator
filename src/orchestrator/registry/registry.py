"""Dynamic tool registry.

Consistency model
-----------------
* All mutations are serialised by an ``asyncio.Lock``.
* The registry holds an immutable mapping of immutable entries. Every mutation builds a new
  mapping and swaps it in atomically, so readers (discovery, routing) always see a consistent
  snapshot and never need the lock.

Registration validates that the spec is well formed, every declared operation has an async
handler, and every input/output schema is valid JSON Schema. Being registered does **not**
authorize anything — authorization is the permission manager's job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from orchestrator.contracts.tools import (
    CapabilityMatch,
    HealthStatus,
    OperationSpec,
    ToolDescriptor,
    ToolSpec,
    ToolState,
)
from orchestrator.errors import (
    DuplicateToolError,
    InputValidationError,
    InvalidToolSpecError,
    OperationNotFoundError,
    OutputValidationError,
    ToolNotFoundError,
    ToolUnavailableError,
)
from orchestrator.observability.audit import AuditLog
from orchestrator.tools.base import ToolAdapter

_MAX_REPORTED_ERRORS = 10


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    adapter: ToolAdapter
    spec: ToolSpec
    state: ToolState
    input_validators: Mapping[str, Draft202012Validator]
    output_validators: Mapping[str, Draft202012Validator]

    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(spec=self.spec, state=self.state)


def _now() -> datetime:
    return datetime.now(UTC)


def _build_validators(spec: ToolSpec) -> tuple[dict[str, Any], dict[str, Any]]:
    inputs: dict[str, Draft202012Validator] = {}
    outputs: dict[str, Draft202012Validator] = {}
    for op in spec.operations:
        for kind, schema, target in (
            ("input", op.input_schema, inputs),
            ("output", op.output_schema, outputs),
        ):
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise InvalidToolSpecError(
                    f"{spec.tool_id}.{op.name}: invalid {kind} schema: {exc.message}",
                    details={"tool_id": spec.tool_id, "operation": op.name, "schema": kind},
                ) from exc
            target[op.name] = Draft202012Validator(schema)
    return inputs, outputs


def _schema_errors(validator: Draft202012Validator, instance: Any) -> list[dict[str, str]]:
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    return [
        {"path": "/".join(str(p) for p in e.absolute_path) or "<root>", "message": e.message[:200]}
        for e in errors[:_MAX_REPORTED_ERRORS]
    ]


class ToolRegistry:
    def __init__(self, audit: AuditLog | None = None, *, health_check_timeout_s: float = 5.0):
        self._tools: Mapping[str, RegisteredTool] = MappingProxyType({})
        self._lock = asyncio.Lock()
        self._audit = audit or AuditLog()
        self._health_timeout = health_check_timeout_s

    # ------------------------------------------------------------------ mutations

    async def register(self, adapter: ToolAdapter, *, replace_existing: bool = False) -> ToolDescriptor:
        spec = getattr(adapter, "spec", None)
        if not isinstance(spec, ToolSpec):
            raise InvalidToolSpecError(f"{type(adapter).__name__} has no ToolSpec 'spec'")
        missing = adapter.missing_handlers()
        if missing:
            raise InvalidToolSpecError(
                f"tool {spec.tool_id!r} declares operations without async handlers: {missing}",
                details={"tool_id": spec.tool_id, "missing": missing},
            )
        inputs, outputs = _build_validators(spec)

        old: RegisteredTool | None = None
        async with self._lock:
            existing = self._tools.get(spec.tool_id)
            if existing is not None:
                if not replace_existing:
                    raise DuplicateToolError(
                        f"tool {spec.tool_id!r} is already registered",
                        details={"tool_id": spec.tool_id, "version": existing.spec.version},
                    )
                if existing.adapter is not adapter:
                    old = existing
            entry = RegisteredTool(
                adapter=adapter,
                spec=spec,
                # A replacement keeps the operator's enabled/disabled choice, but health
                # is re-learned because the implementation changed.
                state=ToolState(
                    enabled=existing.state.enabled if existing else True,
                    registered_at=_now(),
                ),
                input_validators=MappingProxyType(inputs),
                output_validators=MappingProxyType(outputs),
            )
            self._swap({**self._tools, spec.tool_id: entry})

        if old is not None:
            await self._close_quietly(old.adapter)
        self._audit.record(
            "tool.replaced" if existing else "tool.registered",
            tool_id=spec.tool_id,
            version=spec.version,
            capabilities=sorted(spec.capabilities),
        )
        return entry.descriptor()

    async def unregister(self, tool_id: str) -> None:
        async with self._lock:
            entry = self._require(tool_id)
            self._swap({k: v for k, v in self._tools.items() if k != tool_id})
        await self._close_quietly(entry.adapter)
        self._audit.record("tool.unregistered", tool_id=tool_id)

    async def set_enabled(self, tool_id: str, enabled: bool) -> ToolDescriptor:
        async with self._lock:
            entry = self._require(tool_id)
            new = replace(entry, state=entry.state.model_copy(update={"enabled": enabled}))
            self._swap({**self._tools, tool_id: new})
        self._audit.record("tool.enabled" if enabled else "tool.disabled", tool_id=tool_id)
        return new.descriptor()

    async def check_health(self, tool_id: str | None = None) -> dict[str, HealthStatus]:
        """Probe one tool or all tools concurrently. Failures and timeouts mark a tool
        unhealthy; it then drops out of capability discovery until a later check passes."""
        snapshot = self._tools
        if tool_id is not None:
            targets = [self._require(tool_id)]
        else:
            targets = list(snapshot.values())

        async def probe(entry: RegisteredTool) -> tuple[RegisteredTool, HealthStatus, str | None]:
            try:
                ok = await asyncio.wait_for(entry.adapter.health_check(), self._health_timeout)
            except TimeoutError:
                return entry, HealthStatus.UNHEALTHY, f"health check timed out after {self._health_timeout}s"
            except Exception as exc:  # adapter bugs must not crash the registry
                return entry, HealthStatus.UNHEALTHY, f"{type(exc).__name__}: {exc}"[:300]
            if ok:
                return entry, HealthStatus.HEALTHY, None
            return entry, HealthStatus.UNHEALTHY, "health check returned False"

        results = await asyncio.gather(*(probe(e) for e in targets))
        checked_at = _now()
        out: dict[str, HealthStatus] = {}
        async with self._lock:
            tools = dict(self._tools)
            for probed, status, error in results:
                current = tools.get(probed.spec.tool_id)
                # Skip if the tool was unregistered or replaced while we were probing.
                if current is None or current.adapter is not probed.adapter:
                    continue
                tools[probed.spec.tool_id] = replace(
                    current,
                    state=current.state.model_copy(
                        update={"health": status, "last_health_check": checked_at, "last_error": error}
                    ),
                )
                out[probed.spec.tool_id] = status
            self._swap(tools)
        for tid, status in out.items():
            if status == HealthStatus.UNHEALTHY:
                self._audit.record("tool.unhealthy", tool_id=tid, error=tools[tid].state.last_error)
        return out

    # ------------------------------------------------------------------ queries (lock-free)

    def get(self, tool_id: str) -> RegisteredTool:
        return self._require(tool_id)

    def get_available(self, tool_id: str) -> RegisteredTool:
        entry = self._require(tool_id)
        if not entry.state.available:
            reason = "disabled" if not entry.state.enabled else "unhealthy"
            raise ToolUnavailableError(
                f"tool {tool_id!r} is {reason}", details={"tool_id": tool_id, "reason": reason}
            )
        return entry

    def operation(self, tool_id: str, operation: str) -> OperationSpec:
        op = self._require(tool_id).spec.operation(operation)
        if op is None:
            raise OperationNotFoundError(
                f"tool {tool_id!r} has no operation {operation!r}",
                details={"tool_id": tool_id, "operation": operation},
            )
        return op

    def __contains__(self, tool_id: object) -> bool:
        return tool_id in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def list_tools(self, *, include_unavailable: bool = True) -> list[ToolDescriptor]:
        return [
            e.descriptor()
            for _, e in sorted(self._tools.items())
            if include_unavailable or e.state.available
        ]

    def capabilities(self, *, available_only: bool = True) -> dict[str, list[str]]:
        """capability -> sorted list of ``tool_id.operation`` providing it."""
        out: dict[str, list[str]] = {}
        for entry in self._tools.values():
            if available_only and not entry.state.available:
                continue
            for op in entry.spec.operations:
                for cap in op.capabilities:
                    out.setdefault(cap, []).append(f"{entry.spec.tool_id}.{op.name}")
        return {k: sorted(v) for k, v in sorted(out.items())}

    def find_by_capability(self, capability: str, *, available_only: bool = True) -> list[CapabilityMatch]:
        """All (tool, operation) pairs providing ``capability``, best candidates first.

        Ordering is deterministic: availability, reliability (desc), cost, latency, id.
        Final selection policy lives in the router (Phase 03).
        """
        matches: list[CapabilityMatch] = []
        for entry in self._tools.values():
            available = entry.state.available
            if available_only and not available:
                continue
            for op in entry.spec.operations:
                if capability in op.capabilities:
                    matches.append(
                        CapabilityMatch(
                            capability=capability,
                            tool_id=entry.spec.tool_id,
                            operation=op.name,
                            access=op.access,
                            sensitive=op.sensitive,
                            available=available,
                            reliability=entry.spec.reliability,
                            cost=entry.spec.constraints.cost,
                            expected_latency_ms=entry.spec.constraints.expected_latency_ms,
                        )
                    )
        matches.sort(
            key=lambda m: (not m.available, -m.reliability, m.cost, m.expected_latency_ms, m.tool_id, m.operation)
        )
        return matches

    # ------------------------------------------------------------------ schema validation

    def validate_input(self, tool_id: str, operation: str, args: Any) -> None:
        self.operation(tool_id, operation)
        validator = self._require(tool_id).input_validators[operation]
        errors = _schema_errors(validator, args)
        if errors:
            raise InputValidationError(
                f"invalid input for {tool_id}.{operation}",
                details={"tool_id": tool_id, "operation": operation, "errors": errors},
            )

    def validate_output(self, tool_id: str, operation: str, output: Any) -> None:
        self.operation(tool_id, operation)
        validator = self._require(tool_id).output_validators[operation]
        errors = _schema_errors(validator, output)
        if errors:
            raise OutputValidationError(
                f"invalid output from {tool_id}.{operation}",
                details={"tool_id": tool_id, "operation": operation, "errors": errors},
            )

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        async with self._lock:
            entries = list(self._tools.values())
            self._swap({})
        for e in entries:
            await self._close_quietly(e.adapter)

    # ------------------------------------------------------------------ internals

    def _require(self, tool_id: str) -> RegisteredTool:
        entry = self._tools.get(tool_id)
        if entry is None:
            raise ToolNotFoundError(f"tool {tool_id!r} is not registered", details={"tool_id": tool_id})
        return entry

    def _swap(self, tools: dict[str, RegisteredTool]) -> None:
        self._tools = MappingProxyType(tools)

    async def _close_quietly(self, adapter: ToolAdapter) -> None:
        try:
            await adapter.aclose()
        except Exception as exc:
            self._audit.record("tool.close_failed", tool_id=adapter.tool_id, error=str(exc)[:300])
