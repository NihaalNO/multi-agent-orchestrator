"""The spec's MVP demonstration, run in-process.

    python examples/mvp_demo.py                      # offline: rule-based planner
    python examples/mvp_demo.py --planner anthropic  # Claude plans it (needs ANTHROPIC_API_KEY)

Reads examples/data/sales.csv, averages the Revenue column with the Python tool and, independently,
runs a web search. There is no live search backend yet, so the search tool serves two clearly
labelled SAMPLE results; replace StaticSearchBackend with a real SearchBackend to search the web.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from orchestrator.config import Settings
from orchestrator.contracts import AccessLevel, PermissionGrant, Principal
from orchestrator.core import Orchestrator
from orchestrator.observability import AuditLog, MetricsSink
from orchestrator.permissions import ApprovalManager, GrantPolicy, PermissionManager
from orchestrator.registry import ToolRegistry
from orchestrator.routing import RuleBasedPlanner, build_planner
from orchestrator.tools import FileProcessingTool, PythonExecTool, SearchResult, StaticSearchBackend, WebSearchTool

DATA = Path(__file__).resolve().parent / "data"
REQUEST = ("Read the uploaded CSV file sales.csv, calculate its average revenue using Python, "
           "and independently search for the latest industry revenue benchmarks.")

SAMPLE_RESULTS = [
    SearchResult(title="[SAMPLE] Industry revenue benchmarks 2026", url="https://example.com/benchmarks",
                 snippet="Sample data for the demo, not a real search result.", published_at="2026-09-01"),
    SearchResult(title="[SAMPLE] Revenue benchmarks by region", url="https://example.com/benchmarks/?utm_source=demo",
                 snippet="Same page with a tracking parameter: merged as a duplicate.", published_at="2026-09-01"),
]

GRANTS = [
    PermissionGrant(subject="role:analyst", tool_id="file_processing", resources=("file:*",)),
    PermissionGrant(subject="role:analyst", tool_id="python_exec", operation="run", max_access=AccessLevel.EXECUTE),
    PermissionGrant(subject="role:analyst", tool_id="web_search", operation="search"),
]


async def run(planner_name: str) -> int:
    uploads = Path(tempfile.mkdtemp(prefix="orch_demo_"))
    try:
        shutil.copy(DATA / "sales.csv", uploads / "sales.csv")
        metrics = MetricsSink()
        audit = AuditLog(metrics)
        registry = ToolRegistry(audit)
        await registry.register(FileProcessingTool(uploads))
        await registry.register(PythonExecTool())
        await registry.register(WebSearchTool(StaticSearchBackend(SAMPLE_RESULTS)))

        settings = Settings(llm_provider=planner_name)
        planner = RuleBasedPlanner() if planner_name == "rule_based" else build_planner(settings, audit)
        permissions = PermissionManager(GrantPolicy(GRANTS), registry, audit)
        orch = Orchestrator(registry, planner, permissions, ApprovalManager(audit), audit, settings=settings)

        alice = Principal(user_id="alice", roles=frozenset({"analyst"}))
        record = await orch.submit(REQUEST, alice, files=("sales.csv",))

        print(record.summary)
        if record.report is not None:
            print("\nAverage revenue:", json.dumps(record.report.final_output.get("compute_mean", {}).get("result")))
            merged = record.report.resolved.get("merged:web.search", [])
            print(f"Search results after de-duplication: {len(merged)}")
            for item in merged:
                print(f"  - {item['title']}  (found by {', '.join(item['found_by'])})")
        print("\nStage timings (ms):", {s.stage: s.duration_ms for s in record.stages})
        tools = metrics.snapshot()["tools"]
        print("Tool success rates:", {t: m["success_rate"] for t, m in tools.items()})
        return 0 if record.state.value == "completed" else 1
    finally:
        await asyncio.sleep(0)  # let subprocess transports close before removing the folder
        shutil.rmtree(uploads, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--planner", choices=["rule_based", "anthropic"], default="rule_based")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.planner))


if __name__ == "__main__":
    sys.exit(main())
