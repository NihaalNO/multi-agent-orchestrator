# HTTP API reference

The server also publishes an interactive OpenAPI description at `/docs` (Swagger UI) and
`/openapi.json` while it is running. This page explains behaviour the schema cannot.

## Authentication

Every endpoint except `GET /health` and the chat page (`GET /`, `GET /ui`, `GET /ui/{name}`) needs

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
| `files` | Names of uploaded files (see [Files](#files)) that the request may use. |
| `allow_partial` | `true`: run the parts that can be routed and report the rest as caveats. `false` (default): reject the whole request if any part cannot be routed. |
| `context` | Up to 5 ids of **your own completed** earlier requests, oldest first: a follow-up ("now double it") can use their results. They become `R1`, `R2`…; the planner sees their questions, answers and output shapes, and references values with `{"$ref": "@R1.<task_id>.<path>"}`, resolved from the stored record when the step runs. Another user's id (admins included) answers `404` like a missing one; an unfinished or failed request answers `409`. |

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

### `POST /requests/stream`

The same request as `POST /requests` (same body, same `Idempotency-Key` handling), answered as
**server-sent events** while it runs (`text/event-stream`). Read it with `fetch` and the response
body; `EventSource` cannot send the `Authorization` header.

| Event | Data |
|---|---|
| `accepted` | `{"request_id": "…"}`: sent first, before planning starts |
| `progress` | one per change: `received`, `planner_retry`, `planned` (`tasks`), `executing`, `task_started` (`task_id`, `tool`, `operation`), `task_retry`, `task_finished` (`task_id`, `tool`, `status`, `duration_ms`, `deduplicated`, `error` code), `approval_needed`, `replanning` (`attempt`, `failed` task ids), `conflict`, `answering`, `answered` (claim counts), `answer_failed`, `finished` (`state`); each has `at` |
| `record` | `{"status": <the status POST /requests would return>, "record": {…}}`: the last event |
| `error` | `{"status": …, "error": {…}}` instead of `record` if the request could not run |

Progress carries names, statuses and timings only — never arguments, outputs, file paths or user
ids; results come in the final record. A `: keep-alive` comment is sent after 15 s without events.
Closing the stream does **not** cancel the request: fetch it later with `GET /requests/{id}`.

### `POST /requests/{request_id}/replay`

Run a finished request (`completed` or `failed`) **again** and compare every step with the original:
"would this still give the same answer today?" Only the requester may replay (admins get `404`, as for
resume); a request that never ran answers `409`.

- The **stored plan** runs again; nothing is re-planned, so differences come from tools and data.
- Each step runs **for real** (a fresh idempotency key: no cached results) under your **current**
  permissions.
- Only steps that read or compute run again. A step that writes or needs approval is **never
  repeated**, nor anything depending on it; those come back as `not_rerun` with the reason.
- The original record is not changed and the written answer is not re-written.

```json
{"request_id": "…", "replay_id": "…", "replayed_at": "…", "verdict": "differs",
 "counts": {"identical": 1, "differs": 2, "failed": 0, "not_rerun": 0},
 "steps": [{"task_id": "load_sales", "outcome": "differs", "original_status": "succeeded",
            "replay_status": "succeeded", "original_sha256": "sha256:…", "replay_sha256": "sha256:…",
            "changed": ["row_count", "rows.4.Revenue"], "original_ms": 9, "replay_ms": 5,
            "note": "4 value(s) changed"}],
 "receipt_matches_record": true, "policy_changed": false}
```

`verdict`: `identical` (every re-run step gave the same result), `differs`, or `incomplete` (a step
failed or nothing could run again). Fields that change on every run anyway (retrieval time, timings,
printed output) are ignored when comparing. `receipt_matches_record` / `policy_changed` compare the
stored record with its receipt and today's policy with the one in force then (`null` without a receipt).

### `GET /requests/{request_id}/receipt`

The request's **receipt** (owner or admin; others get `404`), issued when the request finishes, once
approvals are settled. It holds hashes, ids and statuses — never data — so it can be shared:
`sequence` and `previous` (the hash of the previous receipt: the chain), `question_sha256`,
`policy_sha256`, `plan_sha256`, one entry per step (`status`, `args_sha256` of the exact arguments
that ran, `output_sha256`, `approval_id`, `approved_by`, times), the answer's text and evidence
hashes with each claim's status, `context` (receipt hashes of earlier requests a follow-up used),
and `key_id` + `signature` (Ed25519 over the receipt without the signature) when the server has a key.

Check it offline: `python -m orchestrator verify-receipt receipt.json --public-key pub.pem --record record.json`
(the record from `GET /requests/{id}`): signature, then every hash recomputed from the record.

### `GET /receipts/public-key`

Public. `{"algorithm": "Ed25519", "key_id": "…", "public_key_pem": "…"}`; `404` if receipts are unsigned.

### `GET /admin/receipts?since=0&limit=500`

Admin. The receipt chain after sequence `since`, for audits:
`python -m orchestrator verify-receipt --chain chain.json --public-key pub.pem` checks that sequences
are consecutive, each links to the previous receipt's hash, and every signature is valid.

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

## Files

Upload files for requests to use. Permissions use the target `tool_id: "uploads"` (operations
`upload`, `list`, `delete`) with the resource `file:<path>`; it is separate from the file-reading
tool, so plans can never write or delete files. With the example policy each analyst owns
`users/<their id>/`.

### `PUT /files/{path:path}`

The request body is the raw file (any client can stream it; no multipart). `?overwrite=true` replaces
an existing file; without it an existing name is a `409`, even when two uploads race.

| Status | Meaning |
|---|---|
| `201` / `200` | created / replaced; body `{"path", "size_bytes", "sha256", "modified"}` |
| `403` | not permitted for this path, or an unsafe path (absolute, `..`, hidden `.name` segments) |
| `409` `file_exists` | name taken and `overwrite` not set |
| `413` `upload_too_large` | over `ORCH_UPLOAD_MAX_BYTES` (checked while streaming; nothing is kept) |
| `415` `unsupported_file_type` | extension not in `ORCH_UPLOAD_EXTENSIONS`, or a text file that is not UTF-8 |

### `GET /files?pattern=*.csv`

Files you may list (`uploads.list` on each file), with size and modification time.

### `DELETE /files/{path:path}`

Delete a file you may delete. `404` if it does not exist. Deletions are audited (`file.deleted`).

## Discovery

| Endpoint | Returns |
|---|---|
| `GET /health` | `{"status": "ok", "version": "…"}` — public, for load balancers and container health checks |
| `GET /ui`, `GET /ui/{name}` | the browser chat page and its `chat.css` / `chat.js` — public static files with a strict CSP; the page asks for your API key and calls the endpoints below. `GET /` redirects here |
| `GET /ui/fonts/{name}` | the chat's self-hosted fonts (Public Sans, Barlow Condensed; SIL OFL, licenses beside them in `static/fonts/`), public, cached for a year |
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
