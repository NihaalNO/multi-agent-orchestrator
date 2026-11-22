# Multi-Tool Orchestrator Agent

A permission-aware orchestrator that discovers tools at runtime, routes tasks by capability,
executes independent work concurrently, and reconciles the results. The LLM proposes plans;
deterministic Python validates, authorizes and executes them.

- Specification: [docs/SPEC.md](docs/SPEC.md)
- Architecture & contracts: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Roadmap & requirement status: [docs/ROADMAP.md](docs/ROADMAP.md)

## Status

Phases 01 (foundation), 02 (dynamic tool registry) and 04 (permissions) are complete; Phase 03
(planning and capability routing) is complete apart from a live web-search backend. Next is
Phase 05 (parallel execution). See the roadmap for details and open decisions.

## Planner configuration

The planner uses Claude (`claude-opus-5-5`) through the official `anthropic` SDK. Credentials
are resolved by the SDK (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login`
profile); `ORCH_LLM_API_KEY` overrides them.

| Variable | Default | Meaning |
|---|---|---|
| `ORCH_LLM_PROVIDER` | `anthropic` | `anthropic` or `rule_based` (offline, deterministic) |
| `ORCH_LLM_MODEL` | `claude-opus-5-5` | Claude model ID |
| `ORCH_LLM_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` |

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
  sensitive operations be *requested* (they still need an approver).
- `config/principals.json` maps users to roles and to the **SHA-256 hash** of their API key. The
  example hashes are placeholders that match no key. Hash a key without echoing it:

```powershell
.\.venv\Scripts\python -c "import hashlib,getpass; print(hashlib.sha256(getpass.getpass('API key: ').encode()).hexdigest())"
```

Roles with special meaning: `admin` (enable/disable tools, health checks) and `approver` (approve
or deny sensitive operations; nobody can approve their own request). Both role names are
configurable (`ORCH_ADMIN_ROLE`, `ORCH_APPROVER_ROLE`).

Run the API:

```powershell
$env:ORCH_POLICY_PATH = "config\policy.json"; $env:ORCH_PRINCIPALS_PATH = "config\principals.json"; .\.venv\Scripts\python -m uvicorn orchestrator.api:create_app --factory --reload
```

Clients send `Authorization: Bearer <api key>`. `/health` is the only public endpoint.

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
