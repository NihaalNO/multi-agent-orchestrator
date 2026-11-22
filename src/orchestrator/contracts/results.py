"""Result contracts: per-task results, detected conflicts, and the final report."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.errors import OrchestratorError


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    DENIED = "denied"
    AWAITING_APPROVAL = "awaiting_approval"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self not in (TaskStatus.PENDING, TaskStatus.RUNNING)


class ErrorInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    recoverable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorInfo":
        if isinstance(exc, OrchestratorError):
            return cls(**exc.to_dict())
        return cls(code="unexpected_error", message=f"{type(exc).__name__}: {exc}")


class TaskResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    tool_id: str | None = None
    operation: str | None = None
    status: TaskStatus
    output: dict[str, Any] | None = None
    error: ErrorInfo | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    approval_id: str | None = Field(default=None, description="Set when the task awaits or used an approval.")
    deduplicated: bool = Field(default=False, description="Result reused from an earlier identical call.")
    resource: str | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds() * 1000
        return None


class ConflictKind(StrEnum):
    CONTRADICTION = "contradiction"
    DUPLICATE = "duplicate"
    FORMAT_MISMATCH = "format_mismatch"
    CONCURRENT_WRITE = "concurrent_write"
    STALE = "stale"
    PARTIAL_FAILURE = "partial_failure"


class Conflict(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ConflictKind
    task_ids: tuple[str, ...]
    description: str
    resolved: bool = False
    resolution: str | None = None
    policy: str | None = Field(default=None, description="Name of the policy that resolved it.")
    escalated: bool = False


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"


class ExecutionReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    execution_id: str
    plan_id: str
    status: ExecutionStatus
    task_results: tuple[TaskResult, ...]
    conflicts: tuple[Conflict, ...] = ()
    final_output: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
