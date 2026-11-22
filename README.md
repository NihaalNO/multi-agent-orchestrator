# Multi-Tool Orchestrator Agent

A permission-aware orchestrator that discovers tools at runtime, routes tasks by capability,
executes independent work concurrently, and reconciles the results. The LLM proposes plans;
deterministic Python validates, authorizes and executes them.

| Document | For |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | the original project specification |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data contracts and design decisions |
| [docs/API.md](docs/API.md) | HTTP endpoints, status codes, the request record |
| [docs/TOOLS.md](docs/TOOLS.md) | writing and registering a new tool |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | configuration, Docker, monitoring, security checklist |
| [docs/ROADMAP.md](docs/ROADMAP.md) | what is done, partial and still open |

## Try it in 30 seconds

No API key or network needed:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python examples\mvp_demo.py
```

This runs the spec's MVP request (read a CSV, average its revenue with Python, search for
benchmarks) through the whole pipeline and prints the summary. `examples/custom_tool.py` adds a tool
at runtime; `examples/approval_workflow.py` walks through a sensitive operation's approval. With an
Anthropic API key set, `examples\mvp_demo.py --planner anthropic --search anthropic` lets Claude do
the planning and run real web searches; with a Gemini key, `--planner gemini --search gemini` does
the same with Google Gemini.

## Status

All eight phases are complete, all ten essential test scenarios from the spec pass, and CI (tests
on Linux and Windows, plus the Docker image) is green. Live web search is available through Claude
or Gemini (`ORCH_SEARCH_BACKEND=anthropic` / `gemini`). Before a production rollout: a first live
run of the planner and search backend with either provider. See the roadmap's overall status.

## Planner configuration

Four LLM providers are supported, plus an offline planner. Every provider gets the same prompt,
the same output schema and the same checks on what it returns.

| Provider | `ORCH_LLM_PROVIDER` | Default model | Credentials |
|---|---|---|---|
| Anthropic (default) | `anthropic` | `claude-opus-5-5` | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `ant auth login` |
| Google Gemini | `gemini` | `gemini-3.8-flash` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`); install with `pip install -e ".[gemini]"` |
| Groq | `groq` | `openai/gpt-oss-20b` | `GROQ_API_KEY`; install with `pip install -e ".[openai-compat]"` |
| Ollama (local) | `ollama` | `gpt-oss:20b` | none locally (`OLLAMA_API_KEY` for Ollama's cloud); needs a running Ollama server and `ollama pull <model>`; same extra as Groq |
| Offline | `rule_based` | — | none |

**Llama models** run through Groq or Ollama: `ORCH_LLM_MODEL=llama-3.3-70b-versatile` (Groq) or
`ORCH_LLM_MODEL=llama3.3` (Ollama). Meta's own API now serves Meta's Muse models, not Llama, so it
is not a provider here. On Groq, Llama models only support JSON mode (not schema-enforced output),
so they are given the schema in their instructions and may produce plans that fail validation more
often; the default `openai/gpt-oss-20b` has strict schema output.

`ORCH_LLM_MODEL` overrides the default model, `ORCH_LLM_API_KEY` overrides the SDK's own key
lookup, and `ORCH_LLM_EFFORT` (`low` … `max`) applies to Claude only. Web search can use either
provider too (`ORCH_SEARCH_BACKEND`); see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

## Permissions and API keys

Everything is denied unless a grant allows it. Copy the examples and edit them:

```powershell
Copy-Item config\policy.example.json config\policy.json; Copy-Item config\principals.example.json config\principals.json
```

- `config/policy.json` lists grants: `subject` (`role:<name>` or `user:<id>`), `tool_id`,
  `operation`, `max_access` (`read` < `write` < `execute` < `admin`), optional `resources` globs
  such as `file:reports/*`, `effect: "deny"` to carve out exceptions, and `allow_sensitive` to let
  sensitive operations be *requested* (they still need an approver). `{user_id}` in a pattern means
  the caller's own id: the example policy gives every analyst a private folder with
  `file:users/{user_id}/*`. Uploads have their own target, `tool_id: "uploads"`.
- `config/principals.json` maps users to roles and to the **SHA-256 hash** of their API key. The
  example hashes are placeholders that match no key. Create a key and its hash (the key is shown
  once; `hash-key` without `--generate` hashes an existing key at a hidden prompt):

```powershell
.\.venv\Scripts\python -m orchestrator hash-key --generate
```

Roles with special meaning: `admin` (enable/disable tools, health checks) and `approver` (approve
or deny sensitive operations; nobody can approve their own request). Both role names are
configurable (`ORCH_ADMIN_ROLE`, `ORCH_APPROVER_ROLE`).

Run the API:

```powershell
$env:ORCH_POLICY_PATH = "config\policy.json"; $env:ORCH_PRINCIPALS_PATH = "config\principals.json"; .\.venv\Scripts\python -m orchestrator check-config; .\.venv\Scripts\python -m orchestrator serve
```

Clients send `Authorization: Bearer <api key>`. `/health` is the only public endpoint.

## Making requests

Upload a file (the body is the raw file; with the example policy, analysts write to
`users/<their id>/`):

```powershell
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8000/files/users/alice/sales.csv -Headers @{ Authorization = "Bearer <your api key>" } -InFile .\sales.csv
```

Then ask about it:

```powershell
$h = @{ Authorization = "Bearer <your api key>"; "Idempotency-Key" = "report-001" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/requests -Headers $h -ContentType "application/json" -Body '{"text": "Read users/alice/sales.csv and calculate its average revenue using Python"}'
```

The response is the request record: `state` (`completed`, `failed`, `awaiting_approval`,
`rejected`), the plan and routing decisions, the execution report with resolved values and
conflicts, `uncertainty` caveats, a plain-text `summary` and per-stage timings. Re-sending the same
`Idempotency-Key` returns the same request instead of running it again.

If a sensitive operation needs approval the response is `202` with `pending_approvals`. An
approver calls `POST /approvals/{approval_id}/approve`; the requester then calls
`POST /requests/{request_id}/resume`, which runs only what has not finished yet.
`GET /admin/metrics` (admin role) reports per-tool success rates, retries and latency.

## Adding a tool

```python
from orchestrator.contracts import AccessLevel, OperationSpec, ToolSpec
from orchestrator.tools import ToolAdapter

class EchoTool(ToolAdapter):
    spec = ToolSpec(
        tool_id="echo",
        name="Echo",
        description="Returns its input",
        operations=(
            OperationSpec(
                name="say",
                description="Echo a message",
                capabilities=("text.echo",),
                input_schema={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
                output_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
                access=AccessLevel.READ,
            ),
        ),
    )

    async def op_say(self, args, ctx):
        return {"msg": args["msg"]}

await registry.register(EchoTool())
```

Every declared operation needs a matching `op_<name>` coroutine, and schemas must be valid JSON
Schema. Registration is rejected otherwise. Registering a tool does not authorize anyone to
use it. If an operation touches an identifiable resource (a file, a record), override
`resource_for(operation, args)` to return a string such as `file:<path>` so grants can be
scoped to it.
