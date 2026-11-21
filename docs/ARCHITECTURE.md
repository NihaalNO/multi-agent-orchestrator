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
   roles to the principal, because none of those values come from the plan.
3. The execution engine re-checks permission immediately before each invocation
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
Principal         user_id, roles            (from authentication, never from the LLM)
PermissionGrant   role/user → tool_id (or *) → operation (or *) → max access level, resource patterns
PermissionDecision allowed, requires_approval, reason, matched_grant
```

Default is **deny**. A grant must explicitly cover the tool, operation, access level and
(if the task names one) the resource.

### 3.6 Errors

One hierarchy rooted at `OrchestratorError`, each with a stable `code` and a `recoverable`
flag. The engine's retry policy reads only `recoverable` — timeouts and transient tool
errors retry; validation, permission, and not-found errors never do. This is what prevents
infinite or pointless retry loops (spec §6.1).

---

## 4. Execution lifecycle mapping

| Spec stage | Where it happens |
|---|---|
| 1 Request intake | API layer → `OrchestratorRequest` |
| 2 Intent analysis, 3 Decomposition | `Planner.plan()` (LLM or rule-based) |
| 4 Capability discovery, 5 Tool selection | `CapabilityRouter.route()` (via `RoutingService.plan_and_route()`) |
| 6 Permission validation | `PermissionPolicy.check()` at plan time **and** before each invoke |
| 7 Execution planning | `ExecutionPlan.layers()` |
| 8 Tool execution | `DagScheduler` → `ExecutionEngine.run_task()` |
| 9 Result validation | engine output-schema check |
| 10 Conflict resolution | `ConflictResolver.resolve()` |
| 11 Aggregation, 12 Response | `Orchestrator` builds `ExecutionReport` (+ optional LLM summary) |

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
| Admin API auth | Admin endpoints need `ORCH_ADMIN_TOKEN`; disabled if unset | Default-deny until full auth lands in Phase 04. |
| Python sandbox | Separate subprocess (`python -I`), timeout, temp working dir, no inherited env | Honest limitation: a subprocess is **not** a security sandbox. Container isolation (Docker) is the planned upgrade; the `isolation` field already models it. |

---

## 6. Open questions (need a decision before the phase that uses them)

1. ~~LLM provider~~ — decided: Anthropic (Claude Opus 5.5).
2. **Web search backend**: a keyed API (Tavily, Brave, SerpAPI), or Anthropic's server-side
   web search tool (same API key as the planner, billed per search)? Tests use the static
   backend regardless; adding one is a single `SearchBackend` class.
3. **Python isolation strength** (Phase 05): is subprocess-level isolation acceptable for
   the MVP, or is Docker available on the target machine?

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
  execution/                engine + DAG scheduler                    (Phase 05)
  resolution/               conflict resolver                         (Phase 06)
  core.py                   Orchestrator facade                       (Phase 07)
  api/app.py                FastAPI application
tests/                      pytest, mirrors src layout
docs/                       SPEC, ARCHITECTURE, ROADMAP
```
