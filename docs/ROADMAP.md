# Roadmap & Requirement Status

Status legend: ✅ done · 🟡 partial · ⬜ not started · ⛔ blocked (see note)

This file is the record of completed, incomplete and blocked requirements required by
spec §13.12. Update it in the same commit as the work it describes.

## Overall status

All eight phases are delivered. Every foundational capability (registry, routing, permissions,
parallel execution, conflict resolution) has independently tested code, and they work together
end to end (spec §13 completion standard): the MVP request runs through `POST /requests`, and all
ten essential test scenarios pass.

Since then: live Groq planning works end to end in the browser chat (`/ui`), with re-planning on
plan errors and a retry for unreadable plans. Phases 09–14 below are the plan for what comes next.

Remaining before a production rollout:

| Item | State | Needs |
|---|---|---|
| Live run of a real planner | ✅ | Groq (`openai/gpt-oss-120b`) through the API and the chat page, 2026-11-22; Anthropic still blocked on account credits |
| Live web search run | ⛔ | built for three backends (`ORCH_SEARCH_BACKEND=anthropic` / `gemini` / `tavily`); needs a working key for one of them. Tavily works alongside the Groq planner |
| Multi-instance deployment | ⬜ | durable state is done (SQLite, single instance); scaling out needs a shared database + distributed lock, when one instance is no longer enough |
| Stronger Python isolation | ✅ | WebAssembly sandbox (Phase 09) |
| LLM-written summaries, OpenTelemetry export | ⬜ | optional extensions (spec §11); MCP tools and the container sandbox are now Phases 13 and 09 |

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

## Phase 03 — Intelligent Routing  ✅

| Item | Status | Where |
|---|---|---|
| File Processing adapter (sandboxed root, traversal/symlink protection, size cap) | ✅ | `tools/file_processing.py` |
| Python Execution adapter (separate `python -I` process, timeout, no inherited env) | ✅ | `tools/python_exec.py` — not a security sandbox, see ARCHITECTURE §5 |
| Web Search adapter + `SearchBackend` interface + static backend | ✅ | `tools/web_search.py` |
| Live web search backend | ✅ | `AnthropicSearchBackend` (Claude server-side web search), opt-in via `ORCH_SEARCH_BACKEND=anthropic`; tested with a fake client — no live search run yet |
| `Planner` protocol, `ToolCatalog`, `RuleBasedPlanner` | ✅ | `routing/planner.py` |
| `AnthropicPlanner` (Claude Opus 5.5, structured outputs, refusal fallbacks, error mapping) | ✅ | `routing/anthropic_planner.py` — tested with a fake client only; **no live API call made yet** |
| `GeminiPlanner` + `GeminiSearchBackend` (Google Gemini as alternative provider; shared prompt/schema/parser) | ✅ | `routing/gemini_planner.py`, `tools/gemini_search.py`; tested with a fake client only, **no live Gemini call made yet** |
| Groq and Ollama planners (`OpenAICompatPlanner`; Llama models via either) | ✅ | `routing/openai_compat_planner.py`; tested with a fake client only, **no live Groq or Ollama call made yet** |
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
| Persistent approval store | ✅ | SQLite with `ORCH_STATE_PATH` (`state.py`) |

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
| Idempotency keys for non-idempotent operations | ✅ | `IdempotencyStore` (TTL; results in memory or SQLite) |
| Per-resource write locks | ✅ | `ResourceLocks` |
| Resuming an execution after approval without re-running other tasks | ✅ | done in Phase 07 (`prior=` results) |

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
| Automatic re-run / extra verification | 🟡 | conflicts still escalate; plan-caused *failures* are now re-planned with error feedback (`repair.py`, `ORCH_MAX_REPLANS`) |
| Human sign-off workflow for escalated conflicts | ⬜ | flagged via `requires_review`; an approval-style flow can reuse Phase 04 approvals if needed |

## Phase 07 — Integration and Testing  ✅

| Item | Status | Where |
|---|---|---|
| `Orchestrator` facade running the 12-stage lifecycle | ✅ | `core.py` (`submit`, `resume`, `get`) |
| Request records with state, plan, routing, authorization, report, summary, stage timings | ✅ | `RequestRecord`; `RequestStore` in memory or SQLite |
| `POST /requests`, `GET /requests/{id}`, `POST /requests/{id}/resume` | ✅ | `api/app.py`; 200 / 202 / error status codes |
| Client idempotency (`Idempotency-Key` header): same key → same request, no re-planning | ✅ | `RequestStore.by_key` |
| Resume after approval without re-running finished tasks | ✅ | `DagScheduler.execute(prior=...)` (closes the Phase 05 gap) |
| Rejections (no tool, denied, planner down) recorded with structured errors; nothing executed | ✅ | `RequestState.REJECTED` |
| Requests never left stuck in `running` (planner bug, cancellation) | ✅ | facade error handling |
| Deterministic plain-text summary of the outcome | ✅ | `build_summary` |
| Latency and reliability measurement | ✅ | `MetricsSink` (per-tool success rate, retries, p50/p95), `GET /admin/metrics`, stage timings per request |
| Built-in tools registered at startup | ✅ | `register_default_tools` (web search only with a real backend) |
| Complete workflows, permission violations, concurrency, conflicts, unavailable tools, partial failures | ✅ | `tests/test_orchestrator.py` + earlier phase tests; all 10 spec scenarios pass |
| Smoke test against a real `uvicorn` server | ✅ | done manually (not automated) |
| Live Claude planner run | ⛔ | needs an Anthropic API key; only tested with a fake client |
| LLM-written result summary | ⬜ | deterministic summary for now; a Claude summarizer can be added behind the same field |
| File upload endpoint | ✅ | `PUT/GET/DELETE /files` (`uploads.py`): streamed size cap, type allow-list, safe paths, atomic no-overwrite commit, per-user folders via `{user_id}` grants |
| Persistent request store | ✅ | SQLite with `ORCH_STATE_PATH`; interrupted requests marked failed on restart, never re-run; tests simulate restarts (`tests/test_state.py`) |

## Phase 08 — Documentation and Deployment  ✅

| Item | Status | Where |
|---|---|---|
| Architecture documented | ✅ | `docs/ARCHITECTURE.md` |
| Tool registration documented | ✅ | `docs/TOOLS.md` |
| API endpoints documented | ✅ | `docs/API.md` (+ live OpenAPI at `/docs`) |
| Deployment configuration | ✅ | `Dockerfile`, `docker-compose.yml`, `.env.example`, `requirements.lock`, `docs/DEPLOYMENT.md` |
| Docker image built and smoke-tested | ✅ | CI builds it, checks it refuses to start without `/config`, then starts it with the example config and probes `/health` and auth (passing since run 44984412929, 2026-11-22). Not built on the development machine (no Docker there). |
| CLI: `serve`, `hash-key`, `check-config` | ✅ | `python -m orchestrator` |
| Example workflows | ✅ | `examples/mvp_demo.py`, `custom_tool.py`, `approval_workflow.py` (all run in tests) |
| Monitoring and logging | ✅ | JSON logs to stdout, optional audit mirroring, `/admin/metrics`, health check |
| CI | ✅ | `.github/workflows/ci.yml`: tests and examples on Linux 3.11, Linux 3.14 and Windows 3.14, plus the Docker job — all passing (run 44984412929). The first run's Docker failure was a CI setup error, fixed in f412135. |
| Docs kept in sync with code | ✅ | `tests/test_docs_and_cli.py` fails if a setting or route is undocumented, a documented setting no longer exists, or a doc link breaks |
| Minimum Python version verified | ✅ | full suite passes on 3.11 (installed from `requirements.lock`) and 3.14 |

---

## Next phases (planned)

Direction: **an agent whose answers can be proven.** The LLM already only proposes while
deterministic code decides, checks permissions and records argument hashes. The phases below build
on that so every answer is linked to its evidence, can be replayed, and runs under rules that can be
tested before they change. Phase 09 is a prerequisite; Phase 10 is the main differentiator.

## Phase 09 — Foundations  ✅

| Item | Status | Notes |
|---|---|---|
| Real Python sandbox | ✅ | WebAssembly (CPython 3.15 for WASI in wasmtime, `IsolationLevel.WASM`): no host files or network, memory and time caps; `tests/test_wasm_sandbox.py`, CI fetches the build on Linux and Windows, Docker image requires it. Verified live with Groq through the chat. Standard library only |
| Live progress in the chat | ✅ | `POST /requests/stream` (server-sent events from the audit stream through `observability/progress.py`, whitelisted fields only); the chat shows a live step checklist. Verified live with Groq + Tavily |
| Conversation memory within permissions | ✅ | `context` on `POST /requests` (and `/stream`): earlier requests become R1…, values flow by `{"$ref": "@R1.task.path"}`; only the user's own completed requests (admins included in the rule), steps authorized as usual, referenced values become answer evidence. Verified live with Groq ("now double it" → 258,650 from `@R1.avg_revenue.result`) |

Done when: code runs in the sandbox by default with the same tests passing, the chat shows steps as
they finish, and a follow-up can use an earlier result without leaking another user's data.

## Phase 10 — Verifiable answers  🟡

| Item | Status | Notes |
|---|---|---|
| Every number linked to its source | 🟡 | grounded answers (`orchestrator/answers.py`): the LLM answer cites numbered evidence and code checks every number against it; unconfirmed claims are marked. Done for Groq/Ollama planners and verified live with Groq + Tavily; Anthropic/Gemini composers to follow |
| Signed receipts | ✅ | `orchestrator/receipts.py`: per request hashes of question, policy, plan, each step's arguments and output, approvals with approver, answer and claim statuses; Ed25519-signed, chained by sequence + previous hash, stored in SQLite; `GET /requests/{id}/receipt`, `/receipts/public-key`, `/admin/receipts`; `verify-receipt` CLI. Verified live: a real Groq run's receipt verified offline, and an edited output was caught |
| Replay | ✅ | `orchestrator/replay.py`, `POST /requests/{id}/replay`, chat Replay button: stored plan re-run under current permissions, side-effect steps never repeated, per-step identical / differs (with changed paths) / failed / not re-run, plus receipt and policy checks. Verified live: unchanged data replayed identically against Groq + Tavily; an edited upload was pinpointed (`rows.4.Revenue`, `result`) |

Done when: the chat shows the source of every number, a receipt verifies offline, and a replay of
an unchanged request reproduces its outputs.

## Phase 11 — Plans people can see and control  ⬜

| Item | Status | Notes |
|---|---|---|
| Preview before running | ⬜ | show tools, files, approvals needed and expected cost; the user edits, approves or cancels |
| Saved workflows | ⬜ | a good plan is saved, reviewed by an admin and rerun without calling the LLM: deterministic, cheaper, more reliable over time |

## Phase 12 — Policy you can test  ⬜

| Item | Status | Notes |
|---|---|---|
| Policy simulation | ⬜ | replay recent requests against a proposed policy: "this change would allow 14 more requests and block 2" |
| Denial explanations | ⬜ | every denial names the missing grant and how to request it |

## Phase 13 — More planners and tools, same rules  ⬜

| Item | Status | Notes |
|---|---|---|
| Planners checking each other | ⬜ | two providers plan independently; the conflict resolver compares plans or answers and disagreement becomes a caveat |
| MCP tools under policy | ⬜ | register tools from MCP servers at runtime, each one going through permissions, approvals and audit |

## Phase 14 — Measuring planner quality  ⬜

| Item | Status | Notes |
|---|---|---|
| Provider scorecards | ⬜ | a fixed set of requests run against each provider and model: first-plan success, re-plans needed, time and cost. Live runs showed `gpt-oss-20b` and `gpt-oss-120b` differ sharply, which only measurement reveals |

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
