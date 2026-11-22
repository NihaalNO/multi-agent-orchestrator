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

## Phase 03 — Intelligent Routing  🟡 (only the live search backend remains)

| Item | Status | Where |
|---|---|---|
| File Processing adapter (sandboxed root, traversal/symlink protection, size cap) | ✅ | `tools/file_processing.py` |
| Python Execution adapter (separate `python -I` process, timeout, no inherited env) | ✅ | `tools/python_exec.py` — not a security sandbox, see ARCHITECTURE §5 |
| Web Search adapter + `SearchBackend` interface + static backend | ✅ | `tools/web_search.py` |
| Live web search backend | ⛔ | blocked on decision: which search provider (ARCHITECTURE §6) |
| `Planner` protocol, `ToolCatalog`, `RuleBasedPlanner` | ✅ | `routing/planner.py` |
| `AnthropicPlanner` (Claude Opus 5.5, structured outputs, refusal fallbacks, error mapping) | ✅ | `routing/anthropic_planner.py` — tested with a fake client only; **no live API call made yet** |
| `CapabilityRouter`: deterministic ranking, proposal re-verification, static input validation | ✅ | `routing/router.py` |
| "No matching tool" handling, incl. partial routing | ✅ | `NoMatchingToolError`, `allow_partial` |
| Permission-aware candidate filtering | ✅ | wired in Phase 04 |
| Cost/latency-weighted scoring beyond lexicographic ranking | ⬜ | deferred until real cost/latency data exists (spec §11) |

## Phase 04 — Permission Management  ✅

| Item | Status | Where |
|---|---|---|
| Grant model: user/role → tool → operation → access level → resource patterns; deny rules | ✅ | `contracts/permissions.py` |
| Default-deny policy engine | ✅ | `permissions/policy.py` (`GrantPolicy`) |
| Resources derived by adapters from real arguments (not from the plan); path normalisation | ✅ | `ToolAdapter.resource_for`, `FileProcessingTool.resource_for` |
| Provisional decisions when a resource depends on an upstream output | ✅ | `PermissionDecision.provisional`; final check is the engine's job (Phase 05) |
| Permission-aware planning catalog, routing filter, plan authorization | ✅ | `PermissionManager`, `RoutingService` |
| Approval workflow: approver role, no self-approval, arg-digest binding, TTL, single use | ✅ | `permissions/approvals.py`; engine integration in Phase 05 |
| Permission decisions and approval transitions audited | ✅ | `permission.decision`, `approval.*` events |
| Principal-based API auth (hashed API keys), admin/approver roles, approval endpoints | ✅ | `api/auth.py`, `api/app.py` |
| Policy / principals loaded from JSON files | ✅ | `permissions/loader.py`, `config/*.example.json` |
| JWT / OAuth 2.0 | ⬜ | not needed for MVP; `ApiKeyAuthenticator` is the swap point |
| Persistent approval store | ⬜ | in-memory for now (approvals are lost on restart) |

## Phase 05 — Parallel Execution  ✅

| Item | Status | Where |
|---|---|---|
| `ExecutionEngine.run_task`: availability → input schema → final permission check → approval → invoke → output schema | ✅ | `execution/engine.py` |
| Final permission check with resolved arguments (settles provisional decisions) | ✅ | engine step 3 |
| Approval enforcement: request on first run, consume (single use, exact args) on re-run | ✅ | engine step 5 |
| Timeouts; retries only for `recoverable` errors, capped, exponential backoff + jitter | ✅ | `ExecutionEngine._attempts` |
| Non-idempotent ops never retried after a timeout | ✅ | only `TransientToolError` retries them |
| `DagScheduler`: ready-queue execution (not layer-by-layer) | ✅ | `execution/scheduler.py` |
| Global and per-tool concurrency limits, shared across executions | ✅ | engine semaphores |
| `$ref` resolution from upstream outputs (dict keys, list indices, deep-copied) | ✅ | `execution/refs.py` |
| Partial failure: dependents `skipped`, independent branches continue | ✅ | scheduler |
| Whole-plan deadline and caller cancellation, propagated into tools | ✅ | scheduler; Python tool kills its child process on cancel |
| Idempotency keys for non-idempotent operations | ✅ | `IdempotencyStore` (in-memory, TTL) |
| Per-resource write locks | ✅ | `ResourceLocks` |
| Resuming an execution after approval without re-running other tasks | ⬜ | re-submitting re-runs idempotent tasks; non-idempotent ones dedupe via idempotency key. Execution state store arrives with the facade (Phase 07). |

## Phase 06 — Conflict Resolution  ✅

| Item | Status | Where |
|---|---|---|
| Output schema validation | ✅ | engine (Phase 05); failures become `failed` tasks |
| Partial-failure reporting (some tasks did not complete) | ✅ | `ConflictKind.PARTIAL_FAILURE` + uncertainty note |
| Duplicate detection: identical calls (same tool, op, argument digest) | ✅ | merged if outputs match, cross-checked if not |
| Duplicate detection: result lists merged by normalised URL | ✅ | `resolved["merged:<capability>"]`, `found_by` per item |
| Cross-checks between tasks the planner put in the same `compare_group` | ✅ | field-by-field comparison with numeric tolerance |
| Format mismatches ("200" vs 200) | ✅ | `normalization` policy |
| Source-priority (registered reliability) and timestamp (recency) policies | ✅ | `source_priority`, `recency`; plus `majority` (≥3 sources) |
| Configurable policy order and thresholds | ✅ | `ResolverConfig` |
| Concurrent writes to the same resource without a dependency ordering them | ✅ | `ConflictKind.CONCURRENT_WRITE`, escalated |
| Staleness of source timestamps | ✅ | `ConflictKind.STALE` (default: older than 365 days) |
| Escalation: unresolved conflicts reported with all candidates, value left unknown | ✅ | `Conflict.escalated`, `ExecutionReport.requires_review` |
| Conflict decisions audited | ✅ | `conflict.detected` / `.resolved` / `.escalated` |
| Planner can request cross-checks | ✅ | `compare_group` in the Claude planner's schema and prompt |
| Automatic re-run / extra verification to break a tie | ⬜ | escalates instead; re-planning belongs to the orchestrator facade (Phase 07) |
| Human sign-off workflow for escalated conflicts | ⬜ | flagged via `requires_review`; an approval-style flow can reuse Phase 04 approvals if needed |

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
| 2 | Request requiring multiple tools | ✅ MVP request planned, routed, authorized and executed across 3 tools (`test_execution.py::test_mvp_request_end_to_end`); API endpoint in Phase 07 |
| 3 | Request with no matching tool | ✅ `tests/test_router.py`, `tests/test_planners.py` |
| 4 | Unauthorized write operation | ✅ `tests/test_permissions.py` (policy + routing) |
| 5 | Three independent tasks concurrently | ✅ `test_execution.py` (timing + observed concurrency) |
| 6 | Sequential dependencies | ✅ `test_execution.py` (start order + `$ref` outputs) |
| 7 | Tool timeout | ✅ `test_execution.py` (retried for idempotent ops, not for writes) |
| 8 | Conflicting tool outputs | ✅ `tests/test_resolution.py` (incl. end-to-end through the scheduler) |
| 9 | Duplicate execution requests | ✅ `test_execution.py` (concurrent + later duplicates run once) |
| 10 | Partial execution failure | ✅ `test_execution.py` (dependents skipped, others succeed) |
