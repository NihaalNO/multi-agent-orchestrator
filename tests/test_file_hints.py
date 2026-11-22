"""Planner sees real CSV column names (header only), and only for files the user may read."""

import pytest

from orchestrator.contracts import AccessLevel, PermissionGrant, PlanningRequest, Principal
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import GrantPolicy, PermissionManager
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RoutingService, build_proposal
from orchestrator.routing.llm_common import SYSTEM_PROMPT, user_message
from orchestrator.routing.planner import ToolCatalog
from orchestrator.tools import FileProcessingTool

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))


class CapturingPlanner:
    name = "capture"

    def __init__(self):
        self.requests = []

    async def plan(self, request, catalog):
        self.requests.append(request)
        return build_proposal(request_id=request.request_id, planner=self.name, tasks=[
            {"task_id": "load", "capability": "data.tabular.load", "inputs": {"path": "users/alice/sales.csv"}}])


@pytest.fixture
async def setup(tmp_path):
    root = tmp_path / "uploads"
    (root / "users/alice").mkdir(parents=True)
    (root / "users/bob").mkdir(parents=True)
    (root / "users/alice/sales.csv").write_text("﻿month,region,Revenue\n2026-01,north,120000\n", encoding="utf-8")
    (root / "users/alice/notes.txt").write_text("hello", encoding="utf-8")
    (root / "users/bob/salaries.csv").write_text("name,Salary\nbob,99999\n", encoding="utf-8")
    reg = ToolRegistry()
    tool = FileProcessingTool(root)
    await reg.register(tool)
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    pm = PermissionManager(GrantPolicy([PermissionGrant(subject="role:analyst", tool_id="file_processing",
                                                        resources=("file:users/{user_id}/*",))]), reg, audit)
    planner = CapturingPlanner()
    return RoutingService(reg, planner, pm, audit), planner, tool, sink


async def test_columns_for_listed_and_mentioned_files(setup):
    service, planner, _, sink = setup
    await service.plan_and_route(PlanningRequest(
        text="average revenue in users/alice/sales.csv", files=("users/alice/sales.csv", "users/alice/notes.txt")), ALICE)
    req = planner.requests[0]
    assert req.file_columns == {"users/alice/sales.csv": ("month", "region", "Revenue")}  # BOM stripped
    assert sink.events("planner.file_hints")[0].data["files"] == ["users/alice/sales.csv"]


async def test_csv_named_only_in_the_text_is_found(setup):
    service, planner, _, _ = setup
    await service.plan_and_route(PlanningRequest(text="average of users/alice/sales.csv please"), ALICE)
    assert planner.requests[0].file_columns == {"users/alice/sales.csv": ("month", "region", "Revenue")}


async def test_nothing_is_revealed_about_files_the_user_may_not_read(setup):
    service, planner, _, sink = setup
    await service.plan_and_route(PlanningRequest(
        text="compare users/bob/salaries.csv with users/alice/sales.csv", files=("users/bob/salaries.csv",)), ALICE)
    cols = planner.requests[0].file_columns
    assert "users/bob/salaries.csv" not in cols and "users/alice/sales.csv" in cols
    assert "Salary" not in user_message(planner.requests[0], ToolCatalog(entries=()))
    # The peek's permission check is not logged as an access; only the plan's own step is authorized.
    resources = [e.data.get("resource") for e in sink.events("permission.decision")]
    assert "file:users/bob/salaries.csv" not in resources


async def test_prompt_shows_header_but_no_data_values(setup):
    service, planner, _, _ = setup
    await service.plan_and_route(PlanningRequest(text="x", files=("users/alice/sales.csv",)), ALICE)
    message = user_message(planner.requests[0], ToolCatalog(entries=()))
    assert "- users/alice/sales.csv (CSV columns: month, region, Revenue)" in message
    assert "120000" not in message and "north" not in message


def test_peek_columns_edge_cases(setup):
    _, _, tool, _ = setup
    root = tool.root
    (root / "empty.csv").write_text("", encoding="utf-8")
    (root / "latin1.csv").write_bytes("caf\xe9,x\n1,2\n".encode("latin-1"))
    assert tool.peek_columns("empty.csv") is None
    assert tool.peek_columns("latin1.csv") is None
    assert tool.peek_columns("users/alice/notes.txt") is None   # not a CSV
    assert tool.peek_columns("missing.csv") is None
    assert tool.peek_columns("../escape.csv") is None          # unsafe paths are refused quietly
    assert tool.peek_columns("users/alice/sales.csv") == ["month", "region", "Revenue"]


def test_planning_rules_forbid_silent_defaults():
    assert "never substitute a default for a missing field" in SYSTEM_PROMPT
    assert "use those exact names" in SYSTEM_PROMPT
