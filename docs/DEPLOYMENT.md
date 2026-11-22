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

`requirements.lock` pins every runtime dependency, including the optional provider SDKs (Gemini,
and `openai` for Groq/Ollama); installing from it reproduces the tested set. A plain
`pip install .` supports Anthropic only; add `".[gemini]"` and/or `".[openai-compat]"` for the others.

## Command line

| Command | Purpose |
|---|---|
| `python -m orchestrator serve [--host] [--port] [--proxy-headers]` | run the API with JSON logging. Refuses to start (exit 1, readable errors) if the settings, policy or principals cannot be loaded. Use `--proxy-headers` only behind a proxy you control. |
| `python -m orchestrator hash-key` | hash a key typed at a hidden prompt (or piped on stdin) |
| `python -m orchestrator hash-key --generate` | create a new random key and print it with its hash |
| `python -m orchestrator check-config` | load settings, policy and principals; print errors and warnings; exit 1 on errors |

`check-config` warns about the common mistakes: no principals (nobody can log in), no grants
(everything denied), no admin or approver, example placeholder hashes still present, and a Claude
planner without credentials. A Gemini planner or search backend without `GEMINI_API_KEY` (or
`GOOGLE_API_KEY`) is an **error**, not a warning: unlike `ant auth login` for Claude, Google's SDK
has no sign-in fallback, so the server would not start.

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
| `ORCH_UPLOAD_MAX_BYTES` | `5242880` (5 MB) | largest accepted upload; matches the file tool's read limit, since a bigger file could not be read |
| `ORCH_UPLOAD_EXTENSIONS` | `csv,txt,md,json` | comma-separated file types accepted by `PUT /files`; text types must be UTF-8 |
| `ORCH_SEARCH_BACKEND` | `none` | `none`: no web search tool. `anthropic`: Claude's server-side web search. `gemini`: Gemini with Google Search grounding. Both are billed per search by the provider |
| `ORCH_SEARCH_MODEL` | provider default | model used for web search (`claude-opus-5-5` / `gemini-3.8-flash`) |
| `ORCH_SEARCH_MAX_USES` | `3` | searches Claude may run for one web search call (1–10; Anthropic only) |
| `ORCH_LLM_PROVIDER` | `anthropic` | `anthropic` (Claude), `gemini` (Google Gemini), `groq`, `ollama` or `rule_based` (offline, deterministic) |
| `ORCH_LLM_MODEL` | provider default | planner model: `claude-opus-5-5` (Anthropic), `gemini-3.8-flash` (Gemini), `openai/gpt-oss-120b` (Groq; `openai/gpt-oss-20b` is faster but makes more plan mistakes), `gpt-oss:20b` (Ollama). Llama: `llama-3.3-70b-versatile` on Groq, `llama3.3` on Ollama |
| `ORCH_LLM_BASE_URL` | provider default | Groq/Ollama endpoint override, e.g. `http://gpu-box:11434/v1` for a remote Ollama or `https://ollama.com/v1` for Ollama's cloud |
| `ORCH_MAX_REPLANS` | `1` | when a run fails because of the plan (code error, wrong field, bad input) on read/compute steps, show the planner the errors and result *shapes* and run its corrected plan, up to this many times. Costs one planner call each; `0` disables; never used for writes, permissions or approvals |
| `ORCH_LLM_MAX_OUTPUT_TOKENS` | `4096` | Groq/Ollama output budget per plan, reasoning included. Groq counts it against your tokens-per-minute limit (8,000 on its on-demand tier), so prompt + budget must stay under that |
| `ORCH_LLM_JSON_MODE` | `auto` | Groq/Ollama output mode. `auto`: schema-enforced output where the provider supports it for the model (Groq: gpt-oss and a few others; local Ollama: any model), otherwise JSON mode with the schema in the instructions. `json_schema` / `json_object` force one |
| `ORCH_LLM_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` (Anthropic only) |
| `ORCH_LLM_API_KEY` | unset | key for the planner's provider (and web search when it uses the same provider). Otherwise each SDK reads its own: `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ant auth login`, or `GEMINI_API_KEY` / `GOOGLE_API_KEY` (Google's SDK prefers `GOOGLE_API_KEY` if both are set), `GROQ_API_KEY`, `OLLAMA_API_KEY` (Ollama's cloud only) |
| `ORCH_POLICY_PATH` | unset | grants file; unset → everything denied |
| `ORCH_PRINCIPALS_PATH` | unset | API-key principals; unset → nobody authenticates |
| `ORCH_ADMIN_ROLE` | `admin` | role name for administration |
| `ORCH_APPROVER_ROLE` | `approver` | role name for deciding approvals |
| `ORCH_APPROVAL_TTL_S` | `3600` | approvals expire after this |
| `ORCH_STATE_PATH` | unset (Docker: `/data/state/orchestrator.db`) | SQLite file for requests, approvals and duplicate protection, so they survive restarts. Unset → kept in memory and lost on restart |
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

## Durable state and restarts

With `ORCH_STATE_PATH` set (the Docker image sets it to `/data/state/orchestrator.db`), request
records, approvals and duplicate-protection records are kept in a SQLite file, so a restart or
redeploy keeps them:

- **Pending approvals** stay pending and can still be approved, then resumed with
  `POST /requests/{id}/resume`; finished tasks are reused, not re-run.
- **Idempotency keys** keep working: re-sending a request with the same `Idempotency-Key` after a
  restart returns the original request.
- **Requests that were running** when the process stopped are marked `failed` with
  `interrupted_by_restart` on the next start. They are **not** re-run automatically: a half-finished
  request may already have had side effects. Re-submit them if appropriate.

The file contains request text, tool outputs and approval previews: protect it like the audit log
(it is created owner-only on Linux). Back it up by copying it while the service is stopped, or with
`sqlite3 orchestrator.db ".backup backup.db"` while running. A database written by a newer version
of the orchestrator is refused at startup rather than misread; `check-config` reports this too.
Metrics and the per-resource write locks stay in memory: they reset on restart by design.

## Scaling

Run **one instance**. The SQLite file and the in-process locks around requests and writes are for
a single process; a second replica must not share the file, and would not see the first one's
in-flight calls. Scaling out needs a shared database plus a distributed lock (for example
PostgreSQL and Redis), deliberately deferred until it is needed.

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
- [ ] Upload grants are scoped (e.g. `file:users/{user_id}/*`) so users cannot read or overwrite
      each other's files; only admins write to shared folders.
- [ ] Rotate a key by replacing its hash and restarting; remove a user by deleting their entry.
