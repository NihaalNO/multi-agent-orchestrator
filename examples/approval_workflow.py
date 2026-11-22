"""Sensitive operations: request → approval by someone else → resume.

    python examples/approval_workflow.py

A user asks to archive and then delete a note. Deleting is marked sensitive, so the request stops
at ``awaiting_approval``. An approver approves; the user resumes, and only the delete runs — the
read that already happened is reused. Over HTTP the same flow is:
POST /requests → 202, POST /approvals/{id}/approve, POST /requests/{id}/resume.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from orchestrator.config import Settings
from orchestrator.contracts import AccessLevel, OperationSpec, PermissionGrant, Principal, ToolSpec, find_refs
from orchestrator.core import Orchestrator
from orchestrator.permissions import ApprovalManager, GrantPolicy, PermissionManager
from orchestrator.registry import ToolRegistry
from orchestrator.routing import build_proposal
from orchestrator.tools import ToolAdapter
from orchestrator.tools.base import RESOURCE_PENDING

NOTE_ID = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
           "additionalProperties": False}


class NotesTool(ToolAdapter):
    spec = ToolSpec(
        tool_id="notes",
        name="Notes",
        description="A small note store.",
        operations=(
            OperationSpec(name="read", description="Read a note", capabilities=("notes.read",),
                          input_schema=NOTE_ID, access=AccessLevel.READ),
            OperationSpec(name="delete", description="Delete a note permanently", capabilities=("notes.delete",),
                          input_schema=NOTE_ID, access=AccessLevel.WRITE, sensitive=True, idempotent=False),
        ),
    )

    def __init__(self) -> None:
        self.notes = {"q3-plan": "Ship the orchestrator."}
        self.reads = 0

    def resource_for(self, operation: str, args: dict[str, Any]):
        note = args.get("id")
        if isinstance(note, str):
            return f"note:{note}"
        return RESOURCE_PENDING if find_refs(note) else None

    async def op_read(self, args, ctx):
        self.reads += 1
        return {"id": args["id"], "text": self.notes.get(args["id"])}

    async def op_delete(self, args, ctx):
        self.notes.pop(args["id"], None)
        return {"deleted": args["id"]}


class FixedPlanner:
    name = "fixed_demo"

    async def plan(self, request, catalog):
        return build_proposal(request_id=request.request_id, planner=self.name, tasks=[
            {"task_id": "read", "capability": "notes.read", "inputs": {"id": "q3-plan"}},
            {"task_id": "delete", "capability": "notes.delete", "inputs": {"id": {"$ref": "read.id"}}},
        ])


async def run() -> int:
    notes = NotesTool()
    registry = ToolRegistry()
    await registry.register(notes)
    policy = GrantPolicy([PermissionGrant(subject="role:editor", tool_id="notes", max_access=AccessLevel.WRITE,
                                          allow_sensitive=True, resources=("note:*",))])
    approvals = ApprovalManager()
    orch = Orchestrator(registry, FixedPlanner(), PermissionManager(policy, registry), approvals,
                        settings=Settings(llm_provider="rule_based"))
    alice = Principal(user_id="alice", roles=frozenset({"editor"}))
    boss = Principal(user_id="boss", roles=frozenset({"approver"}))

    record = await orch.submit("read the q3 plan, then delete it", alice)
    print(f"1. submitted: {record.state.value}; waiting on {record.pending_approvals}")

    approval = approvals.get(record.pending_approvals["delete"])
    print(f"2. approver sees: {approval.tool_id}.{approval.operation} on {approval.resource}, args {approval.args_preview}")
    await approvals.decide(approval.approval_id, approver=boss, approve=True, note="ok, plan is done")

    done = await orch.resume(record.request_id, alice)
    print(f"3. resumed: {done.state.value}; notes left: {list(notes.notes)}; reads performed: {notes.reads}")
    return 0 if done.state.value == "completed" and not notes.notes and notes.reads == 1 else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
