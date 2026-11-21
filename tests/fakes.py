"""Test adapters. Small, deterministic tools used across the test suite."""

from __future__ import annotations

import asyncio
from typing import Any

from orchestrator.contracts import (
    AccessLevel,
    ExecutionConstraints,
    ExecutionContext,
    OperationSpec,
    ToolSpec,
)
from orchestrator.tools.base import ToolAdapter

NUMBERS_IN = {
    "type": "object",
    "properties": {"values": {"type": "array", "items": {"type": "number"}, "minItems": 1}},
    "required": ["values"],
    "additionalProperties": False,
}
MEAN_OUT = {
    "type": "object",
    "properties": {"mean": {"type": "number"}},
    "required": ["mean"],
}


def make_spec(tool_id: str = "calc", *, reliability: float = 0.5, cost: float = 0.0, version: str = "1.0.0") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=f"{tool_id} tool",
        description="Computes statistics",
        version=version,
        reliability=reliability,
        constraints=ExecutionConstraints(cost=cost),
        operations=(
            OperationSpec(
                name="mean",
                description="Arithmetic mean",
                capabilities=("data.compute", "data.stats.mean"),
                input_schema=NUMBERS_IN,
                output_schema=MEAN_OUT,
                access=AccessLevel.EXECUTE,
            ),
        ),
    )


class CalcTool(ToolAdapter):
    spec = make_spec()

    def __init__(self, spec: ToolSpec | None = None, *, healthy: bool = True) -> None:
        if spec is not None:
            self.spec = spec  # instance override for per-test variations
        self.healthy = healthy
        self.closed = False

    async def op_mean(self, args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        values = args["values"]
        return {"mean": sum(values) / len(values)}

    async def health_check(self) -> bool:
        return self.healthy

    async def aclose(self) -> None:
        self.closed = True


class MissingHandlerTool(ToolAdapter):
    spec = make_spec("broken")  # declares "mean" but implements nothing


class SlowHealthTool(CalcTool):
    async def health_check(self) -> bool:
        await asyncio.sleep(10)
        return True


class CrashingHealthTool(CalcTool):
    async def health_check(self) -> bool:
        raise RuntimeError("backend down")
