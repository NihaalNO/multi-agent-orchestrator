"""Adding your own tool: define it, register it at runtime, grant access, use it.

    python examples/custom_tool.py

No orchestrator code changes: the new tool is discovered by capability, routed to, authorized and
executed like the built-in ones. See docs/TOOLS.md for the full guide.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from orchestrator.config import Settings
from orchestrator.contracts import (
    AccessLevel,
    ExecutionConstraints,
    OperationSpec,
    PermissionGrant,
    Principal,
    ToolSpec,
)
from orchestrator.core import Orchestrator
from orchestrator.errors import ToolExecutionError
from orchestrator.permissions import ApprovalManager, GrantPolicy, PermissionManager
from orchestrator.registry import ToolRegistry
from orchestrator.routing import build_proposal
from orchestrator.tools import ToolAdapter

# Fixed demo rates. A real adapter would call a rates API and raise TransientToolError on
# timeouts/rate limits so the engine can retry.
RATES = {("USD", "EUR"): 0.92, ("EUR", "USD"): 1.087, ("USD", "INR"): 84.1}


class CurrencyTool(ToolAdapter):
    spec = ToolSpec(
        tool_id="currency",
        name="Currency Converter",
        description="Converts amounts between currencies using daily reference rates.",
        reliability=0.8,
        constraints=ExecutionConstraints(timeout_s=5, max_retries=2, expected_latency_ms=100),
        operations=(
            OperationSpec(
                name="convert",
                description="Convert an amount from one currency to another.",
                capabilities=("finance.currency.convert",),
                input_schema={
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number"},
                        "from": {"type": "string", "pattern": "^[A-Z]{3}$"},
                        "to": {"type": "string", "pattern": "^[A-Z]{3}$"},
                    },
                    "required": ["amount", "from", "to"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"amount": {"type": "number"}, "currency": {"type": "string"},
                                   "rate": {"type": "number"}, "as_of": {"type": "string"}},
                    "required": ["amount", "currency", "rate", "as_of"],
                },
                access=AccessLevel.READ,
            ),
        ),
    )

    async def op_convert(self, args: dict[str, Any], ctx) -> dict[str, Any]:
        rate = RATES.get((args["from"], args["to"]))
        if rate is None:
            raise ToolExecutionError(f"no rate for {args['from']}->{args['to']}")  # not retried
        return {"amount": round(args["amount"] * rate, 2), "currency": args["to"], "rate": rate,
                "as_of": "2026-11-21"}


class ConvertPlanner:
    """A tiny deterministic planner for 'convert <amount> <CUR> to <CUR>'. Real deployments use
    the Claude planner, which reads the tool's description and schema from the catalog."""

    name = "convert_demo"
    _pattern = re.compile(r"convert\s+([\d.]+)\s+([A-Za-z]{3})\s+(?:to|into)\s+([A-Za-z]{3})", re.I)

    async def plan(self, request, catalog):
        m = self._pattern.search(request.text)
        tasks = []
        if m and "finance.currency.convert" in catalog.capabilities:
            tasks = [{"task_id": "convert", "capability": "finance.currency.convert",
                      "inputs": {"amount": float(m[1]), "from": m[2].upper(), "to": m[3].upper()}}]
        return build_proposal(request_id=request.request_id, planner=self.name, tasks=tasks,
                              unsupported=[] if tasks else [request.text])


async def run() -> int:
    registry = ToolRegistry()
    permissions = PermissionManager(
        GrantPolicy([PermissionGrant(subject="role:finance", tool_id="currency", operation="convert")]), registry
    )
    orch = Orchestrator(registry, ConvertPlanner(), permissions, ApprovalManager(),
                        settings=Settings(llm_provider="rule_based"))
    bea = Principal(user_id="bea", roles=frozenset({"finance"}))

    before = await orch.submit("convert 250 USD to EUR", bea)
    print(f"Before registering the tool: {before.state.value} ({before.error.code})")

    await registry.register(CurrencyTool())  # runtime registration, no restart
    print("Registered. Capabilities now:", registry.capabilities())

    after = await orch.submit("convert 250 USD to EUR", bea)
    print(f"After: {after.state.value}; result = {after.report.final_output['convert']}")

    outsider = Principal(user_id="olly", roles=frozenset({"marketing"}))
    denied = await orch.submit("convert 250 USD to EUR", outsider)
    # The tool is hidden from users who may not use it, so the planner never plans with it; the
    # error says the gap may be a permissions one without revealing which tools exist.
    print(f"Someone without a grant: {denied.state.value} ({denied.error.code}: {denied.error.message})")
    return 0 if after.state.value == "completed" and denied.state.value == "rejected" else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
