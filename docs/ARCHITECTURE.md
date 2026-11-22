# Architecture — Multi-Tool Orchestrator Agent

This document is the analysis and design response to the project seed specification
(`docs/SPEC.md`). It defines the component boundaries, the dependency order between
components, the data contracts they exchange, and the decisions taken where the
specification left room for interpretation.

---

## 1. Guiding constraint: intelligence vs. authority

The specification's core rule is that the LLM *proposes* and the engine *decides*.
This is enforced structurally, not by convention:

| Concern | Owner | Trust level |
|---|---|---|
| Understanding the request, proposing a plan, interpreting results | Planner (LLM or rule-based) | **Untrusted** — output is a proposal |
| Validating the plan, binding tools, checking permissions, scheduling | Orchestrator core (deterministic Python) | Trusted |
| Performing one declared operation | Tool adapter | Trusted to do only what it declares |
| Validating, de-duplicating, reconciling outputs | Result processor | Trusted |

Consequences that the code enforces:

1. A planner returns an `ExecutionPlan` that the orchestrator **re-validates from scratch**
   (tool exists, operation exists, operation actually provides the requested capability,
   dependencies exist, graph is acyclic, inputs match the operation's JSON Schema).
2. Permissions are evaluated by the `PermissionPolicy` using only the authenticated
   `Principal` and the operation's *registered* metadata — never fields the LLM wrote.
   An LLM cannot raise an operation's access level, mark it non-sensitive, or attach
   roles to the principal, because none of those values come from the plan. The
   **resource** being checked (e.g. `file:reports/q1.csv`) is derived by the tool adapter
   from the call's actual arguments (`ToolAdapter.resource_for`), so a plan cannot claim
   to touch one file while reading another.
3. Permission is checked in layers: the planner's catalog only lists operations the principal
   could ever use; the router only selects candidates permitted for each task's inputs; the
   bound plan is authorized task by task; and the execution engine re-checks permission
   immediately before each invocation
   (not only at planning time), so a plan that was valid when built but is no longer
   authorized (tool disabled, grant revoked) is still stopped.

---

## 2. Components and dependency order

```
            contracts + errors            (pure data, no behaviour)
                    │
        ┌───────────┼──────────────┐
        ▼           ▼              ▼
   observability  registry ◄── tool adapters (base class)
     (audit)        │
        │     ┌─────┼──────────────┐
        │     ▼     ▼              ▼
        │  router  permissions   result validation
        │     │     │              │
        │     ▼     ▼              ▼
        │   planner → execution engine → conflict resolution
        │                 │
        │                 ▼
        │           DAG scheduler
        │                 │
        └────────► orchestrator facade ◄── API layer (FastAPI)
```

Build order follows the arrows: nothing depends on a component built after it.
This is also why the roadmap puts the registry (Phase 02) before routing (03),
and permissions (04) before the scheduler (05) — the engine cannot be written
without both.

### Component summary

| # | Component | Module | Responsibility | Depends on |
|---|---|---|---|---|
| A | API layer | `orchestrator.api` | HTTP entry point, request validation, admin endpoints | facade |
| B | Orchestrator core | `orchestrator.core` | Runs the 12-stage lifecycle end-to-end | all below |
| C | Tool registry | `orchestrator.registry` | Register / discover / enable / disable / health-check tools; schema validation | contracts, adapters |
| D | Router | `orchestrator.routing` | Capability → candidate (tool, operation); scoring; plan validation | registry |
| E | Permission manager | `orchestrator.permissions` | Deterministic RBAC + operation-level + resource-scoped decisions, approvals | contracts |
| F | Execution engine | `orchestrator.execution.engine` | Validate input → authorize → invoke with timeout/retry → validate output | registry, permissions, audit |
| G | Scheduler | `orchestrator.execution.scheduler` | DAG layering, concurrency limits, dependency gating, cancellation | engine |
| H | Conflict resolution | `orchestrator.resolution` | Duplicates, contradictions, staleness, source priority, escalation | contracts |
| I | Observability | `orchestrator.observability` | Structured audit events, secret redaction | contracts |

---

## 3. Core data contracts

All contracts are immutable Pydantic v2 models in `orchestrator.contracts`.

### 3.1 Tool metadata (`ToolSpec`)

A tool exposes one or more **operations**. Capabilities, schemas, and permission
requirements are declared **per operation**, because the spec's permission matrix is
operation-level (GitHub *read repo* vs. *create issue* are the same tool, different rights).

```
ToolSpec
  tool_id            unique, ^[a-z][a-z0-9_]*$
  name, description, version
  transport          inprocess | http | mcp       (execution method)
  endpoint           optional URL for non-inprocess transports
  constraints        ExecutionConstraints
  reliability        0..1  (source priority for routing + conflict resolution)
  operations[]       OperationSpec

OperationSpec
  name
  capabilities[]     e.g. "web.search", "data.compute", "file.read"
  input_schema       JSON Schema (validated with jsonschema, Draft 2020-12)
  output_schema      JSON Schema
  access             read | write | execute | admin   (required permission level)
  sensitive          true → explicit human approval required
  idempotent         false → duplicate-execution protection applies
  timeout_s          optional override of tool default

ExecutionConstraints
  timeout_s, max_retries, max_concurrency, cost, expected_latency_ms, isolation
```

Runtime state (enabled flag, health) is **not** part of `ToolSpec`: the spec is what a tool
*declares*; `ToolState` is what the orchestrator *observes*. Mixing them would let a tool
declare itself healthy.

### 3.2 Adapters (`ToolAdapter`)

```python
class ToolAdapter(ABC):
    spec: ToolSpec
    async def op_<operation_name>(self, args: dict, ctx: ExecutionContext) -> dict
    async def health_check(self) -> bool
```

Each declared operation must have a matching `op_<name>` coroutine; the registry rejects
adapters that declare an operation they don't implement. Adding a tool = writing one
adapter class and registering it. No orchestrator code changes.

### 3.3 Plans (`ExecutionPlan`, `TaskSpec`)

```
TaskSpec
  task_id
  capability          what is needed (always set)
  tool_id/operation   optional binding proposed by planner; router fills or validates
  inputs              dict; values may be {"$ref": "<task_id>.<dot.path>"}
  depends_on[]        explicit deps; $ref targets are merged in automatically
  resource            optional resource identifier, used for scoping + write-locking
```

`ExecutionPlan.layers()` returns a deterministic topological layering (Kahn's algorithm);
tasks in the same layer have no dependency on each other and may run concurrently.
Cycles raise `PlanCycleError`.

### 3.4 Results (`TaskResult`, `ExecutionReport`)

`TaskResult` carries status (`succeeded | failed | denied | awaiting_approval | skipped |
cancelled | timed_out`), output, structured `ErrorInfo(code, message, recoverable)`,
attempts, and timings. `ExecutionReport` aggregates task results, detected `Conflict`s and
an overall status of `succeeded | partial | failed`. Partial is first-class: the report
says which parts are missing rather than hiding them.

### 3.5 Permissions

```
Principal          user_id, roles           (from API-key authentication, never from the LLM)
PermissionGrant    subject (role:x | user:y) → tool_id (or *) → operation (or *)
                   effect allow|deny, max_access, resource globs, allow_sensitive
PermissionDecision allowed, requires_approval, provisional, reason, matched_grant
ApprovalRequest    one sensitive call: user, request, task, tool, op, resource,
                   SHA-256 of canonical args, status, expiry, decided_by
```

Evaluation order: matching deny rules win → allow rules must cover the registered access level
and the adapter-derived resource → sensitive operations additionally need `allow_sensitive` and
then a human approval → otherwise **deny**. Resource globs match case-insensitively so deny rules
cannot be sidestepped by case on case-insensitive filesystems. When a resource depends on an
upstream output, resource-scoped rules match *provisionally* and the engine must re-check with
the real value.

Approvals are decided only by the approver role, never by the requester, expire after a TTL, are
single-use, and are bound to the exact arguments by digest — approving one call cannot authorize
a different one.

### 3.6 Errors

One hierarchy rooted at `OrchestratorError`, each with a stable `code` and a `recoverable`
flag. The engine's retry policy reads only `recoverable` — timeouts and transient tool
errors retry; validation, permission, and not-found errors never do. This is what prevents
infinite or pointless retry loops (spec §6.1).

---

## 4. Execution lifecycle mapping

| Spec stage | Where it happens |
|---|---|
| 1 Request intake | `POST /requests` → `Orchestrator.submit()` (validation, idempotency key, request record) |
| 2 Intent analysis, 3 Decomposition | `Planner.plan()` (LLM or rule-based) |
| 4 Capability discovery, 5 Tool selection | `CapabilityRouter.route()` (via `RoutingService.plan_and_route()`) |
| 6 Permission validation | `PermissionPolicy.check()` at plan time **and** before each invoke |
| 7 Execution planning | `ExecutionPlan` dependency graph (`layers()` for inspection; the scheduler uses a ready queue) |
| 8 Tool execution | `DagScheduler.execute()` → `ExecutionEngine.run_task()` |
| 9 Result validation | engine output-schema check; failures and their skipped dependents are reported per task |
| 10 Conflict resolution | `ConflictResolver.resolve(plan, report)` → conflicts, reconciled values, uncertainty notes |
| 11 Aggregation, 12 Response | `Orchestrator` stores a `RequestRecord`: plan, routing, authorization, resolved report, summary, stage timings |

---

## 5. Decisions where the spec left room

| Decision | Choice | Reason |
|---|---|---|
| Orchestration framework | Plain `asyncio` DAG scheduler, **no LangGraph** for the MVP | Spec §4 and §13.7 favour the simplest thing that works. Our graph is a static DAG built per request; LangGraph's value (cyclic agent loops, checkpointing) isn't needed yet. Revisit if we add iterative re-planning. |
| Schema format | JSON Schema, validated with `jsonschema` | Works for in-process, HTTP and MCP tools alike (MCP tools already publish JSON Schema). Pydantic models can emit it via `model_json_schema()`. |
| Persistence | In-memory registry, copy-on-write snapshots | Spec says add PostgreSQL/Redis only when required. Readers always see a consistent snapshot. |
| LLM provider | **Anthropic, `claude-opus-5-5`**, behind the `Planner` protocol; `RuleBasedPlanner` as offline alternative (`ORCH_LLM_PROVIDER=rule_based`) | Every non-LLM part stays testable without network or keys. |
| Planner output format | Structured outputs (`output_config.format` JSON Schema); task inputs as a JSON *string* | The reply is guaranteed to parse; inputs vary per tool, so they are validated against the real operation schema by the router instead of in the LLM schema. |
| Planner refusals | Server-side refusal fallbacks enabled (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`) | A safety-classifier decline is retried on a fallback model in the same call; a final refusal becomes a `PlanError`. |
| Planner effort | `medium` (configurable via `ORCH_LLM_EFFORT`) | Opus 5.5's default; planning is short structured output. Raise if plan quality needs it. |
| Untrusted plan handling | Router re-verifies every proposed binding; invalid ones are overridden with a recorded reason | A hallucinated or injected tool name can never be executed; the audit log shows the override. |
| API auth | Hashed API keys mapped to principals (`ORCH_PRINCIPALS_PATH`); `admin` / `approver` roles | Simple, deterministic and default-deny (no principals → nobody authenticates). JWT/OAuth can replace `ApiKeyAuthenticator` later without touching policy code. |
| Policy storage | JSON grant file (`ORCH_POLICY_PATH`), loaded at startup | Reviewable in version control; no database needed yet. No file → no grants → everything denied. |
| Approval storage | In-memory `ApprovalManager` | Spec §4 (no premature infrastructure). Consequence: approvals do not survive a restart. |
| Python sandbox | Separate subprocess (`python -I`), timeout, temp working dir, no inherited env; killed on cancellation | Honest limitation: a subprocess is **not** a security sandbox. Container isolation (Docker) is the planned upgrade; the `isolation` field already models it. |
| Scheduling strategy | Ready queue: a task starts when its own dependencies succeed | Layer-by-layer execution would make every task wait for the slowest task of the previous layer. |
| Retry safety | Non-idempotent operations retry only on `TransientToolError`, never after a timeout | After a timeout the write may already have happened; retrying could apply it twice. |
| Duplicate protection | Idempotency key (per request) → non-idempotent calls run once per (user, key, tool, op, args) for the TTL; only successes are cached | Covers client double-submits and concurrent duplicates. Two identical non-idempotent calls inside one plan under one key also run once — the conservative choice. |
| Write coordination | In-process per-resource `asyncio` locks | Enough for a single process. Multiple orchestrator processes would need a distributed lock (Redis), deferred per spec §4. |
| What gets compared | Only tasks the planner put in the same `compare_group`, identical calls, and URL-keyed result lists | Comparing arbitrary outputs of different tasks would invent conflicts between answers to different questions. The planner marks deliberate cross-checks; it cannot use the field to affect permissions or tool choice. |
| Resolution policy order | normalization → majority (≥3 sources) → recency → source priority, configurable | Cheapest certain fix first; registered reliability is the weakest evidence, so it comes last and needs a clear margin (0.2). |
| Unresolvable conflicts | Escalated with every candidate; the value is reported as `None` | Spec §2.5: never silently fabricate a resolution. `ExecutionReport.requires_review` tells the caller a human should look. |
| Request records | In-memory `RequestStore`; one record per request, updated in place on resume | Enough for a single process and the MVP; a database is the swap point when restarts must not lose requests. |
| Metrics source | Derived from audit events by an `AuditSink` | One source of truth: metrics can never disagree with the audit log. |
| Result summary | Deterministic text built from the report | Reproducible and testable; an LLM summary can be layered on later without changing the record shape. |
| Default web search | Not registered without a real backend | A tool that always returns nothing would turn "not searched" into "nothing found". The gap is reported as a caveat instead. |
| Approval timing | Approvals are requested and consumed at execution time, with resolved arguments | The approved digest must match the exact arguments, which may depend on upstream outputs unknown at plan time. |

---

## 6. Open questions (need a decision before the phase that uses them)

1. ~~LLM provider~~ — decided: Anthropic (Claude Opus 5.5).
2. **Web search backend**: a keyed API (Tavily, Brave, SerpAPI), or Anthropic's server-side
   web search tool (same API key as the planner, billed per search)? Tests use the static
   backend regardless; adding one is a single `SearchBackend` class.
3. **Python isolation strength**: subprocess isolation is implemented. Is that acceptable for
   the MVP, or should code run in Docker (needs Docker on the target machine)?

---

## 7. Repository layout

```
src/orchestrator/
  config.py                 Settings (env-driven)
  errors.py                 Error hierarchy
  contracts/                Pydantic data contracts
    tools.py  plan.py  results.py  permissions.py  context.py
  observability/audit.py    Audit log + redaction
  registry/registry.py      Dynamic tool registry
  tools/base.py             ToolAdapter base class
  tools/…                   web_search, python_exec, file_processing  (Phase 03)
  routing/                  router + planners                         (Phase 03)
  permissions/              policy engine + approvals                 (Phase 04)
  execution/                engine, DAG scheduler, $ref resolution,
                            idempotency store, resource locks        (Phase 05)
  resolution/               conflict detection + resolution policies  (Phase 06)
  core.py                   Orchestrator facade, request store        (Phase 07)
  observability/metrics.py  latency/reliability metrics from audit events
  api/app.py                FastAPI application
  __main__.py               CLI: serve, hash-key, check-config        (Phase 08)
  observability/logging.py  JSON logging to stdout                    (Phase 08)
tests/                      pytest, mirrors src layout; docs/CLI/example checks
examples/                   runnable workflows (MVP, custom tool, approvals)
config/                     example policy and principals
docs/                       SPEC, ARCHITECTURE, ROADMAP, API, TOOLS, DEPLOYMENT
Dockerfile, docker-compose.yml, requirements.lock, .github/workflows/ci.yml
```
