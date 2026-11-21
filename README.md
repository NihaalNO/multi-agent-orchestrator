# Multi-Tool Orchestrator Agent

A permission-aware orchestrator that discovers tools at runtime, routes tasks by capability,
executes independent work concurrently, and reconciles the results. The LLM proposes plans;
deterministic Python validates, authorizes and executes them.

- Specification: [docs/SPEC.md](docs/SPEC.md)
- Architecture & contracts: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Roadmap & requirement status: [docs/ROADMAP.md](docs/ROADMAP.md)

## Status

Phases 01 (foundation) and 02 (dynamic tool registry) are complete. See the roadmap for what
is next and which decisions are still open.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

Run the API (admin endpoints need `ORCH_ADMIN_TOKEN`):

```powershell
$env:ORCH_ADMIN_TOKEN = "change-me"; .\.venv\Scripts\python -m uvicorn orchestrator.api:create_app --factory --reload
```

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
use it.
