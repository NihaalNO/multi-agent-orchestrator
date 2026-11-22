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


# --- Permission helpers -------------------------------------------------------------------------

from orchestrator.contracts import PermissionGrant, Principal  # noqa: E402
from orchestrator.permissions import GrantPolicy, PermissionManager  # noqa: E402

TESTER = Principal(user_id="tester", roles=frozenset({"tester"}))


def permissive(registry, audit=None) -> PermissionManager:
    """A manager that lets role:tester do anything non-sensitive."""
    return PermissionManager(
        GrantPolicy([PermissionGrant(subject="role:tester", max_access=AccessLevel.ADMIN)]), registry, audit
    )


ID_IN = {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "value": {"type": "string"}},
    "required": ["id"],
    "additionalProperties": False,
}

RECORDS_SPEC = ToolSpec(
    tool_id="records",
    name="Records DB",
    description="A tiny record store",
    operations=(
        OperationSpec(name="read", description="Read a record", capabilities=("db.read",),
                      input_schema=ID_IN, access=AccessLevel.READ),
        OperationSpec(name="update", description="Update a record", capabilities=("db.write",),
                      input_schema=ID_IN, access=AccessLevel.WRITE, idempotent=False),
        OperationSpec(name="delete", description="Delete a record", capabilities=("db.delete",),
                      input_schema=ID_IN, access=AccessLevel.WRITE, sensitive=True, idempotent=False),
    ),
)


class RecordsTool(ToolAdapter):
    """Write-capable tool with a sensitive operation and resource derivation."""

    spec = RECORDS_SPEC

    def __init__(self) -> None:
        self.data: dict[int, str] = {1: "one", 2: "two"}

    def resource_for(self, operation, args):
        from orchestrator.contracts import find_refs
        from orchestrator.tools.base import RESOURCE_PENDING

        rid = args.get("id")
        if isinstance(rid, int):
            return f"db:records/{rid}"
        return RESOURCE_PENDING if find_refs(rid) else None

    async def op_read(self, args, ctx):
        return {"id": args["id"], "value": self.data.get(args["id"])}

    async def op_update(self, args, ctx):
        self.data[args["id"]] = args.get("value", "")
        return {"ok": True}

    async def op_delete(self, args, ctx):
        self.data.pop(args["id"], None)
        return {"ok": True}
