"""Tool metadata contracts.

``ToolSpec`` is what a tool *declares* about itself. ``ToolState`` is what the orchestrator
*observes* at runtime (enabled flag, health). They are kept separate so a tool can never
declare itself available or authorized.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
CAPABILITY_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"


class AccessLevel(StrEnum):
    """Ordered access levels. A grant of a higher level implies the lower ones."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ACCESS_RANK[self]

    def covers(self, required: "AccessLevel") -> bool:
        return self.rank >= required.rank


_ACCESS_RANK = {
    AccessLevel.READ: 0,
    AccessLevel.WRITE: 1,
    AccessLevel.EXECUTE: 2,
    AccessLevel.ADMIN: 3,
}


class Transport(StrEnum):
    INPROCESS = "inprocess"
    HTTP = "http"
    MCP = "mcp"


class IsolationLevel(StrEnum):
    NONE = "none"
    SUBPROCESS = "subprocess"
    WASM = "wasm"            # WebAssembly sandbox: no host files or network, memory and time caps
    CONTAINER = "container"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExecutionConstraints(_Frozen):
    timeout_s: float = Field(default=30.0, gt=0, le=600)
    max_retries: int = Field(default=0, ge=0, le=10)
    max_concurrency: int = Field(default=4, ge=1, le=256)
    cost: float = Field(default=0.0, ge=0, description="Relative cost units, used by routing.")
    expected_latency_ms: int = Field(default=1000, ge=0)
    isolation: IsolationLevel = IsolationLevel.NONE


class OperationSpec(_Frozen):
    name: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str = Field(min_length=1)
    capabilities: tuple[str, ...] = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    access: AccessLevel = AccessLevel.READ
    sensitive: bool = Field(default=False, description="Requires explicit human approval.")
    idempotent: bool = True
    timeout_s: float | None = Field(default=None, gt=0, le=600)

    @model_validator(mode="after")
    def _check_capabilities(self) -> "OperationSpec":
        import re

        pattern = re.compile(CAPABILITY_PATTERN)
        bad = [c for c in self.capabilities if not pattern.fullmatch(c)]
        if bad:
            raise ValueError(f"invalid capability names: {bad}")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("duplicate capabilities in operation")
        return self


class ToolSpec(_Frozen):
    tool_id: str = Field(pattern=IDENTIFIER_PATTERN)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    version: str = "1.0.0"
    transport: Transport = Transport.INPROCESS
    endpoint: str | None = None
    operations: tuple[OperationSpec, ...] = Field(min_length=1)
    constraints: ExecutionConstraints = ExecutionConstraints()
    reliability: float = Field(
        default=0.5, ge=0, le=1, description="Source priority for routing and conflict resolution."
    )
    tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "ToolSpec":
        names = [op.name for op in self.operations]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate operation names in tool {self.tool_id!r}")
        if self.transport != Transport.INPROCESS and not self.endpoint:
            raise ValueError(f"transport {self.transport} requires an endpoint")
        return self

    def operation(self, name: str) -> OperationSpec | None:
        for op in self.operations:
            if op.name == name:
                return op
        return None

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(c for op in self.operations for c in op.capabilities)

    def timeout_for(self, operation: str) -> float:
        op = self.operation(operation)
        if op is not None and op.timeout_s is not None:
            return op.timeout_s
        return self.constraints.timeout_s


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class ToolState(_Frozen):
    enabled: bool = True
    health: HealthStatus = HealthStatus.UNKNOWN
    last_health_check: datetime | None = None
    last_error: str | None = None
    registered_at: datetime

    @property
    def available(self) -> bool:
        # UNKNOWN counts as available: a tool is not penalised before its first check.
        return self.enabled and self.health != HealthStatus.UNHEALTHY


class ToolDescriptor(_Frozen):
    """Read-only view combining declared spec and observed state (for discovery / API)."""

    spec: ToolSpec
    state: ToolState

    @property
    def available(self) -> bool:
        return self.state.available


class CapabilityMatch(_Frozen):
    """One (tool, operation) pair that provides a requested capability."""

    capability: str
    tool_id: str
    operation: str
    access: AccessLevel
    sensitive: bool
    available: bool
    reliability: float
    cost: float
    expected_latency_ms: int
