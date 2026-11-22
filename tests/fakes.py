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


# --- Execution test tool ------------------------------------------------------------------------

import time as _time  # noqa: E402

from orchestrator.errors import ToolExecutionError, TransientToolError  # noqa: E402

ANY_OBJ = {"type": "object"}


def worker_spec(tool_id="worker", *, timeout_s=2.0, max_retries=2, max_concurrency=8):
    return ToolSpec(
        tool_id=tool_id,
        name="Worker",
        description="Configurable test worker",
        constraints=ExecutionConstraints(timeout_s=timeout_s, max_retries=max_retries, max_concurrency=max_concurrency),
        operations=(
            OperationSpec(name="work", description="Sleep then echo", capabilities=("test.work",),
                          input_schema=ANY_OBJ, output_schema=ANY_OBJ, access=AccessLevel.READ),
            OperationSpec(name="write", description="Non-idempotent write", capabilities=("test.write",),
                          input_schema=ANY_OBJ, output_schema=ANY_OBJ, access=AccessLevel.WRITE, idempotent=False),
            OperationSpec(name="bad_output", description="Violates output schema", capabilities=("test.bad",),
                          input_schema=ANY_OBJ,
                          output_schema={"type": "object", "required": ["needed"]}, access=AccessLevel.READ),
        ),
    )


class WorkerTool(ToolAdapter):
    """Args: label, delay (s), fail_transient (n first attempts), fail_hard, crash, key (resource)."""

    spec = worker_spec()

    def __init__(self, spec=None):
        if spec is not None:
            self.spec = spec
        self.calls: list[tuple[str, int]] = []
        self.events: list[tuple[str, str, float]] = []
        self.active = 0
        self.max_active = 0
        self.active_by_resource: dict[str, int] = {}
        self.max_by_resource: dict[str, int] = {}
        self.cancelled: list[str] = []

    def resource_for(self, operation, args):
        key = args.get("key")
        return f"res:{key}" if isinstance(key, str) else None

    async def _run(self, op, args, ctx):
        label = str(args.get("label", ctx.task_id))
        self.calls.append((label, ctx.attempt))
        res = self.resource_for(op, args)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if res:
            self.active_by_resource[res] = self.active_by_resource.get(res, 0) + 1
            self.max_by_resource[res] = max(self.max_by_resource.get(res, 0), self.active_by_resource[res])
        self.events.append(("start", label, _time.monotonic()))
        try:
            if ctx.attempt <= args.get("fail_transient", 0):
                raise TransientToolError(f"transient failure {ctx.attempt}")
            if args.get("fail_hard"):
                raise ToolExecutionError("hard failure")
            if args.get("crash"):
                raise RuntimeError("adapter bug")
            await asyncio.sleep(args.get("delay", 0))
            return {"label": label, "echo": args.get("echo"), "attempt": ctx.attempt}
        except asyncio.CancelledError:
            self.cancelled.append(label)
            raise
        finally:
            self.active -= 1
            if res:
                self.active_by_resource[res] -= 1
            self.events.append(("end", label, _time.monotonic()))

    async def op_work(self, args, ctx):
        return await self._run("work", args, ctx)

    async def op_write(self, args, ctx):
        return await self._run("write", args, ctx)

    async def op_bad_output(self, args, ctx):
        return {"wrong": True}
