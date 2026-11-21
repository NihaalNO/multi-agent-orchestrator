# Roadmap & Requirement Status

Status legend: ✅ done · 🟡 partial · ⬜ not started · ⛔ blocked (see note)

This file is the record of completed, incomplete and blocked requirements required by
spec §13.12. Update it in the same commit as the work it describes.

---

## Phase 01 — Foundation and Architecture  ✅

| Item | Status | Where |
|---|---|---|
| Repository structure | ✅ | `src/orchestrator/`, `tests/`, `docs/` |
| Core interfaces | ✅ | `ToolAdapter`, `AuditSink`, `PermissionPolicy` / `Planner` protocols |
| Tool metadata contracts | ✅ | `contracts/tools.py` |
| Plan / result / permission / context contracts | ✅ | `contracts/` |
| Configuration management | ✅ | `config.py` (`ORCH_*` env vars) |
| Error-handling conventions | ✅ | `errors.py` (stable `code`, `recoverable` flag) |
| Basic API endpoints | ✅ | `api/app.py` — health, tool listing, capabilities, admin enable/disable |

## Phase 02 — Dynamic Tool Registry  ✅

| Item | Status | Where |
|---|---|---|
| Runtime registration / unregistration / replacement | ✅ | `ToolRegistry.register/unregister` |
| Capability metadata + discovery | ✅ | `find_by_capability`, `capabilities()` |
| Enable / disable at runtime | ✅ | `set_enabled` |
| Unhealthy-tool detection | ✅ | `check_health` (failures + timeouts mark unhealthy) |
| Duplicate / conflicting registration prevention | ✅ | `DuplicateToolError`, handler & schema checks |
| Input / output schema validation | ✅ | `validate_input`, `validate_output` |
| Registry consistency under concurrency | ✅ | lock-serialised writes, copy-on-write snapshots |

## Phase 03 — Intelligent Routing  ⬜

- ⬜ Initial adapters: Web Search, Python Execution, File Processing
- ⬜ `Planner` protocol + `RuleBasedPlanner` (deterministic, test-friendly)
- ⬜ `LLMPlanner` — **needs decision: LLM provider + API key**
- ⬜ `CapabilityRouter`: candidate scoring (availability, reliability, cost, latency)
- ⬜ Plan validation: re-verify every LLM-proposed binding against the registry
- ⬜ "No matching tool" handling (test scenario 3)

## Phase 04 — Permission Management  ⬜

- ⬜ Grant model: user/role → tool → operation → access level → resource patterns
- ⬜ Default-deny `PolicyPermissionManager`
- ⬜ Approval workflow for `sensitive` operations (`awaiting_approval` status)
- ⬜ Permission decisions written to audit log
- ⬜ Replace admin-token stopgap with principal-based auth on the API

## Phase 05 — Parallel Execution  ⬜

- ⬜ `ExecutionEngine.run_task`: input validation → authorize → invoke → output validation
- ⬜ Timeouts, retry policy driven by `recoverable`, retry cap
- ⬜ `DagScheduler`: layer-by-layer + ready-queue execution, global & per-tool concurrency limits
- ⬜ `$ref` input resolution from upstream outputs
- ⬜ Cancellation; downstream tasks of failed tasks marked `skipped`
- ⬜ Idempotency keys for non-idempotent operations; per-resource write locks

## Phase 06 — Conflict Resolution  ⬜

- ⬜ Duplicate detection, contradiction detection on comparable fields
- ⬜ Source-priority (reliability) and timestamp policies
- ⬜ Escalation: unresolved conflicts reported, never silently resolved

## Phase 07 — Integration and Testing  ⬜

- ⬜ `Orchestrator` facade wiring the 12-stage lifecycle
- ⬜ `POST /requests` endpoint
- ⬜ End-to-end MVP demo (CSV average + web benchmark)
- ⬜ All 10 essential test scenarios from spec §9

## Phase 08 — Documentation and Deployment  ⬜

- ⬜ Tool-authoring guide, API reference, example workflows, deployment config

---

## Spec §9 essential test scenarios

| # | Scenario | Status |
|---|---|---|
| 1 | Register a new tool while running | ✅ `tests/test_registry.py`, `tests/test_api.py` |
| 2 | Request requiring multiple tools | ⬜ Phase 07 |
| 3 | Request with no matching tool | ⬜ Phase 03 |
| 4 | Unauthorized write operation | ⬜ Phase 04 |
| 5 | Three independent tasks concurrently | ⬜ Phase 05 |
| 6 | Sequential dependencies | 🟡 ordering logic tested (`ExecutionPlan.layers`); execution in Phase 05 |
| 7 | Tool timeout | 🟡 health-check timeout tested; execution timeout in Phase 05 |
| 8 | Conflicting tool outputs | ⬜ Phase 06 |
| 9 | Duplicate execution requests | ⬜ Phase 05 |
| 10 | Partial execution failure | ⬜ Phase 05 |
