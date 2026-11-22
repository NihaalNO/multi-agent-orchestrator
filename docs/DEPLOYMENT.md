# Deployment and operations

## Quick start (Docker)

> CI (`.github/workflows/ci.yml`) builds and smoke-tests this image on every push to `main` and
> every pull request. The `docker compose` flow below has not been run end to end by hand.
> The image expects `/config/policy.json` and `/config/principals.json` to be mounted and will not
> start without them.

```powershell
Copy-Item .env.example .env                      # then set ANTHROPIC_API_KEY
Copy-Item config\policy.example.json config\policy.json
Copy-Item config\principals.example.json config\principals.json
docker compose run --rm orchestrator python -m orchestrator hash-key --generate   # once per user
docker compose run --rm orchestrator python -m orchestrator check-config
docker compose up -d
```

Put each generated hash in `config/principals.json` and give the key to its user. Uploads and the
audit log live in `./data`; the policy and principals are mounted read-only from `./config`. The
port is bound to `127.0.0.1` — expose it through a reverse proxy that terminates TLS.

## Without Docker

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install --no-deps .
.\.venv\Scripts\python -m orchestrator check-config
.\.venv\Scripts\python -m orchestrator serve --host 127.0.0.1 --port 8000
```

`requirements.lock` pins every runtime dependency; installing from it reproduces the tested set.

## Command line

| Command | Purpose |
|---|---|
| `python -m orchestrator serve [--host] [--port] [--proxy-headers]` | run the API with JSON logging. Refuses to start (exit 1, readable errors) if the settings, policy or principals cannot be loaded. Use `--proxy-headers` only behind a proxy you control. |
| `python -m orchestrator hash-key` | hash a key typed at a hidden prompt (or piped on stdin) |
| `python -m orchestrator hash-key --generate` | create a new random key and print it with its hash |
| `python -m orchestrator check-config` | load settings, policy and principals; print errors and warnings; exit 1 on errors |

`check-config` warns about the common mistakes: no principals (nobody can log in), no grants
(everything denied), no admin or approver, example placeholder hashes still present, and a Claude
planner without credentials.

## Configuration

All settings are environment variables. Invalid values stop the server at startup with a
`configuration_error`.

| Variable | Default | Meaning |
|---|---|---|
| `ORCH_ENVIRONMENT` | `development` | free-form label shown by `check-config` |
| `ORCH_MAX_CONCURRENCY` | `8` | tool calls running at once, across all requests |
| `ORCH_MAX_RETRIES_CAP` | `3` | ceiling over every tool's own `max_retries` |
| `ORCH_HEALTH_CHECK_TIMEOUT_S` | `5` | a health check slower than this marks the tool unhealthy |
| `ORCH_RETRY_BASE_DELAY_S` | `0.2` | first retry delay; doubles per attempt, with jitter |
| `ORCH_RETRY_MAX_DELAY_S` | `5` | longest delay between retries |
| `ORCH_IDEMPOTENCY_TTL_S` | `86400` | how long a non-idempotent call's result is remembered for duplicate protection |
| `ORCH_EXECUTION_DEADLINE_S` | unset | time limit for a whole plan; unfinished tasks are cancelled |
| `ORCH_FILE_TOOL_ROOT` | `data/uploads` | the only folder the file tool can read |
| `ORCH_PYTHON_EXEC_TIMEOUT_S` | `10` | Python snippets are killed after this |
| `ORCH_SEARCH_BACKEND` | `none` | `none`: no web search tool. `anthropic`: Claude's server-side web search, using the planner's credentials (billed per search plus tokens) |
| `ORCH_SEARCH_MAX_USES` | `3` | searches Claude may run for one web search call (1–10) |
| `ORCH_LLM_PROVIDER` | `anthropic` | `anthropic` (Claude) or `rule_based` (offline, deterministic) |
| `ORCH_LLM_MODEL` | `claude-opus-5-5` | Claude model ID |
| `ORCH_LLM_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` |
| `ORCH_LLM_API_KEY` | unset | overrides the SDK's own lookup (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ant auth login`) |
| `ORCH_POLICY_PATH` | unset | grants file; unset → everything denied |
| `ORCH_PRINCIPALS_PATH` | unset | API-key principals; unset → nobody authenticates |
| `ORCH_ADMIN_ROLE` | `admin` | role name for administration |
| `ORCH_APPROVER_ROLE` | `approver` | role name for deciding approvals |
| `ORCH_APPROVAL_TTL_S` | `3600` | approvals expire after this |
| `ORCH_AUDIT_LOG_PATH` | unset | append audit events as JSON lines to this file |
| `ORCH_AUDIT_TO_LOG` | `false` | also write audit events to the application log (stdout) |
| `ORCH_LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error` |

## Monitoring and logging

| Signal | Where |
|---|---|
| Liveness | `GET /health` (public); the Docker `HEALTHCHECK` uses it |
| Application logs | stdout, one JSON object per line (`ts`, `level`, `logger`, `message`) |
| Audit trail | `ORCH_AUDIT_LOG_PATH` (JSONL), optionally mirrored to stdout. Every registration, routing decision, permission decision, approval transition, task start/finish/retry, conflict and request outcome, with request/execution/task ids. Values under secret-looking keys are redacted. |
| Metrics | `GET /admin/metrics`: request states and p50/p95 latency; per tool: call count, statuses, success rate, retries, p50/p95/max latency; conflict counts by kind and outcome |
| Per request | the record's `stages` (time spent planning, executing, resolving) and per-task durations |

Worth alerting on: a tool's success rate dropping, `planner_unavailable` rejections, rising p95
latency, approvals piling up (`GET /approvals`), and `requires_review` requests.

Metrics are in-memory and reset on restart. Exporting them to OpenTelemetry/Prometheus is a
planned extension; the audit stream they are built from is already durable if
`ORCH_AUDIT_LOG_PATH` is set.

## Scaling

Run **one instance**. Requests, approvals, idempotency records and write locks are held in memory,
so a second replica would not see the first one's approvals or duplicates, and restarts lose
in-flight requests and pending approvals. Scaling out needs a shared store for those (a database
plus a distributed lock such as Redis) — deliberately deferred until it is needed.

## Security checklist

- [ ] TLS in front of the service; the container binds to localhost only.
- [ ] Every API key generated with `hash-key --generate`; only hashes stored; placeholder hashes removed.
- [ ] Grants are minimal; sensitive operations go through approvers who are not the requesters.
- [ ] `.env` and the real `config/*.json` are not committed (both are gitignored).
- [ ] **The Python tool is not a security sandbox.** It runs code in a separate process with a time
      limit, a temporary folder and no inherited secrets, but that code can still read files the
      service account can read and use the network. Grant `python_exec` only to trusted users, or
      run it in a container (`IsolationLevel.CONTAINER` is reserved for that).
- [ ] The file tool is limited to `ORCH_FILE_TOOL_ROOT`; keep nothing sensitive there.
- [ ] Rotate a key by replacing its hash and restarting; remove a user by deleting their entry.
