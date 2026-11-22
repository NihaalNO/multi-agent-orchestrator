# HTTP API reference

The server also publishes an interactive OpenAPI description at `/docs` (Swagger UI) and
`/openapi.json` while it is running. This page explains behaviour the schema cannot.

## Authentication

Every endpoint except `GET /health` needs

```
Authorization: Bearer <api key>
```

The key is looked up by its SHA-256 hash in the principals file (`ORCH_PRINCIPALS_PATH`), which
gives the caller a user id and roles. Roles never come from the request.

| Response | Meaning |
|---|---|
| `401` | missing, malformed or unknown key |
| `403` | authenticated, but the endpoint needs a role you do not have, or a policy denied the operation |

Special roles (names configurable): **admin** — manage tools, view metrics, view anyone's requests;
**approver** — list and decide approvals.

## Errors

Errors share one shape. `code` is stable and safe to branch on; `recoverable: true` means trying
again later may succeed (for example `planner_unavailable`).

```json
{"error": {"code": "permission_denied", "message": "...", "recoverable": false, "details": {}}}
```

Rejected *requests* (below) are the exception: their body is the full request record, with the
error inside it.

---

## Requests

### `POST /requests`

Plan, route, authorize, execute and reconcile a natural-language request.

```json
{"text": "Read sales.csv and calculate its average revenue using Python",
 "files": ["sales.csv"],
 "allow_partial": false}
```

| Field | Notes |
|---|---|
| `text` | 1–20,000 characters. Treated as untrusted data by the planner. |
| `files` | Names of files already in the upload folder that the request may use. |
| `allow_partial` | `true`: run the parts that can be routed and report the rest as caveats. `false` (default): reject the whole request if any part cannot be routed. |

Optional header **`Idempotency-Key`** (≤ 200 chars): re-sending the same key (same user) returns
the existing request instead of planning and running it again.

| Status | `state` | Meaning |
|---|---|---|
| `200` | `completed` | Everything, or part of it, succeeded — check `report.status` (`succeeded` / `partial`) and `uncertainty` |
| `200` | `failed` | Execution ran but no task succeeded |
| `202` | `awaiting_approval` | A sensitive operation needs approval; see `pending_approvals` |
| `403` | `rejected` | `permission_denied` |
| `422` | `rejected` | `no_matching_tool`, `plan_error`, `input_validation_error`, … — nothing was executed |
| `503` | `rejected` | `planner_unavailable` (retry later) or `configuration_error` (e.g. bad LLM credentials) |

### The request record

Returned by all request endpoints.

```jsonc
{
  "request_id": "25a2f950…",
  "user_id": "alice",
  "state": "completed",               // running | rejected | awaiting_approval | completed | failed
  "text": "…", "files": ["sales.csv"],
  "planner": "anthropic",
  "plan": {"tasks": [{"task_id": "load_file", "capability": "data.tabular.load",
                      "tool_id": "file_processing", "operation": "read_csv",
                      "inputs": {"path": "sales.csv"}, "depends_on": [], "compare_group": null}]},
  "routing": [{"task_id": "load_file", "tool_id": "file_processing", "operation": "read_csv",
               "rationale": "best of 1 available candidate(s): …", "proposal_rejected": null}],
  "unrouted": [], "unsupported": [],
  "authorization": {"decisions": {"load_file": {"allowed": true, "reason": "allowed by grant for role:analyst"}}},
  "report": {
    "status": "succeeded",            // succeeded | partial | failed | awaiting_approval
    "task_results": [{"task_id": "load_file", "status": "succeeded", "output": {"…": "…"},
                      "attempts": 1, "deduplicated": false, "error": null}],
    "final_output": {"compute_mean": {"result": {"column": "Revenue", "mean": 200.0, "count": 3}}},
    "resolved": {"merged:web.search": [{"url": "…", "found_by": ["web_search"]}]},
    "conflicts": [{"kind": "contradiction", "field": "mean", "escalated": true, "candidates": ["…"]}],
    "uncertainty": ["Not attempted: web_search (…)."]
  },
  "error": null,                      // set when state is rejected / failed
  "summary": "Request 25a2…: completed.\n- load_file [file_processing.read_csv]: succeeded, 8 ms\n…",
  "stages": [{"stage": "plan_route_authorize", "duration_ms": 1.6}, {"stage": "execute", "duration_ms": 218.7},
             {"stage": "resolve", "duration_ms": 0.5}, {"stage": "total", "duration_ms": 220.9}],
  "executions": 1,
  "pending_approvals": {},            // task_id -> approval_id
  "requires_review": false            // true if any conflict was escalated
}
```

Task statuses: `succeeded`, `failed`, `timed_out`, `denied`, `awaiting_approval`, `skipped`
(an upstream task did not succeed), `cancelled` (deadline or cancellation).

### `GET /requests/{request_id}`

The record. Visible to the user who made it and to admins; anyone else gets `404` (the API never
confirms that someone else's request exists).

### `POST /requests/{request_id}/resume`

Continue a request in `awaiting_approval` once its approvals are decided. Tasks that already
succeeded are reused, not re-run. Only the requester can resume (admins can view, not act).

| Status | Meaning |
|---|---|
| `200` / `202` | as for `POST /requests` |
| `409` `approval_required` | some approvals are still pending |
| `409` `invalid_request_state` | the request is not awaiting approval (already done, running, rejected) |
| `404` | not your request |

A denied or expired approval does not block resuming: that task ends `denied` and the request
completes as `partial`.

---

## Approvals

### `GET /approvals?status=pending` — approver role

Approvals in a status (`pending` by default; also `approved`, `denied`, `expired`, `consumed`).
Each shows who asked, the tool, operation, resource and a redacted preview of the exact arguments.

### `GET /approvals/{approval_id}`

The requester and approvers can see it; others get `404`.

### `POST /approvals/{approval_id}/approve` and `POST /approvals/{approval_id}/deny` — approver role

Optional body `{"note": "why"}` (≤ 1000 chars). Approvers cannot decide their own requests. An
approval is single-use, expires (`ORCH_APPROVAL_TTL_S`), and is valid only for the exact arguments
shown. Deciding twice → `409`.

---

## Discovery

| Endpoint | Returns |
|---|---|
| `GET /health` | `{"status": "ok", "version": "…"}` — public, for load balancers and container health checks |
| `GET /me` | your user id and roles |
| `GET /tools` | every registered tool with its spec and runtime state (`enabled`, `health`, `available`) |
| `GET /tools?capability=web.search` | ranked `(tool, operation)` candidates for a capability |
| `GET /tools/{tool_id}` | one tool |
| `GET /capabilities` | capability → `tool.operation` providers (available ones by default) |

Discovery shows what is *registered*, not what you are *allowed* to use.

## Administration — admin role

| Endpoint | Effect |
|---|---|
| `POST /admin/tools/{tool_id}/disable` | stop routing to a tool (in-flight calls finish; new ones fail with `tool_unavailable`) |
| `POST /admin/tools/{tool_id}/enable` | route to it again |
| `POST /admin/tools/health-check?tool_id=…` | run health checks now (all tools if `tool_id` is omitted); unhealthy tools drop out of routing |
| `GET /admin/metrics` | request states and latency, executions, per-tool counts / success rate / retries / p50 / p95 latency, conflicts |

Tools are registered in code (see [TOOLS.md](TOOLS.md)), not over HTTP.
