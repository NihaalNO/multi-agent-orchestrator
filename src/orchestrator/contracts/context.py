"""Execution context passed to every adapter invocation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from orchestrator.contracts.permissions import Principal


class ExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    execution_id: str
    task_id: str
    principal: Principal
    attempt: int = 1
    idempotency_key: str | None = None
    resource: str | None = None  # adapter-derived (ToolAdapter.resource_for), never plan-supplied
