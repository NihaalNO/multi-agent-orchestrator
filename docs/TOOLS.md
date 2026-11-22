# Writing a tool

A tool is one Python class. Registering it makes it discoverable by capability, routable,
authorizable and executable — no orchestrator code changes. A complete runnable example is
[`examples/custom_tool.py`](../examples/custom_tool.py).

## 1. Declare the spec

```python
from orchestrator.contracts import AccessLevel, ExecutionConstraints, OperationSpec, ToolSpec
from orchestrator.tools import ToolAdapter

class CurrencyTool(ToolAdapter):
    spec = ToolSpec(
        tool_id="currency",                      # unique, lowercase identifier
        name="Currency Converter",
        description="Converts amounts between currencies using daily reference rates.",
        reliability=0.8,                         # 0..1, used to rank tools and settle conflicts
        constraints=ExecutionConstraints(
            timeout_s=5,                         # per call
            max_retries=2,                       # capped globally by ORCH_MAX_RETRIES_CAP
            max_concurrency=4,                   # simultaneous calls to this tool
            cost=0.0, expected_latency_ms=100,   # routing tie-breakers
        ),
        operations=(
            OperationSpec(
                name="convert",
                description="Convert an amount from one currency to another.",
                capabilities=("finance.currency.convert",),
                input_schema={...},              # JSON Schema (Draft 2020-12)
                output_schema={...},
                access=AccessLevel.READ,         # read < write < execute < admin
                sensitive=False,                 # True: every call needs human approval
                idempotent=True,                 # False: duplicate protection, no retry after timeouts
            ),
        ),
    )
```

**Descriptions and schemas are what the Claude planner reads** to decide whether and how to use
your tool, so write them for a reader who has never seen the code: say what the operation does,
what each input means, and use `description`, `enum`, `pattern` and `required` in the schema.

### Choosing the right flags

| Field | Set it to | Because |
|---|---|---|
| `capabilities` | dotted names, most general first: `data.compute`, `web.search`, `finance.currency.convert` | Tasks ask for capabilities, not tools. Reuse existing names when your tool does the same job (it then competes on reliability/cost); invent a new one only for a new kind of work. |
| `access` | the *least* level that describes the effect | Grants are matched against this. A read that you label `write` locks out readers; a write labelled `read` is a security bug. |
| `sensitive` | `True` for deletions, payments, sending messages, anything hard to undo | Calls stop at `awaiting_approval` until someone else approves the exact arguments. |
| `idempotent` | `False` if running twice differs from running once | The engine then deduplicates calls with an idempotency key, holds a per-resource lock, and never retries after a timeout (the first attempt may have taken effect). |
| `reliability` | lower for scraped/web data, higher for authoritative sources | Used when two tools disagree. |

Registration is rejected if an operation has no handler, names are duplicated, or a schema is not
valid JSON Schema.

## 2. Implement one coroutine per operation

```python
    async def op_convert(self, args: dict, ctx) -> dict:
        rate = await self._rates.get(args["from"], args["to"])
        return {"amount": round(args["amount"] * rate, 2), "currency": args["to"],
                "rate": rate, "as_of": rate_date.isoformat()}
```

* The method is named `op_<operation name>`.
* `args` has already been validated against `input_schema`; your return value is validated
  against `output_schema` (a mismatch fails the task).
* `ctx` carries `request_id`, `task_id`, `principal`, `attempt`, `idempotency_key` and `resource`.
  Pass `idempotency_key` on to downstream APIs that support it.
* Include a source timestamp (`as_of`, `published_at`, `updated_at`) when your data has one: it
  powers the `recency` conflict policy and staleness warnings.
* Do **not** check permissions in the tool. The engine has already done it.

### Errors decide retries

```python
from orchestrator.errors import ToolExecutionError, TransientToolError

raise TransientToolError("rate limited")        # retried (also for non-idempotent ops: it did NOT take effect)
raise ToolExecutionError("unknown currency")    # not retried
```

Timeouts are applied by the engine. Unexpected exceptions are treated as non-recoverable bugs.
If your tool holds external resources (processes, sockets), clean them up when the coroutine is
cancelled — the engine cancels on timeouts and deadlines.

## 3. Declare the resource you touch

If an operation acts on an identifiable thing (a file, a record, a repository), override
`resource_for` so grants can be scoped to it and concurrent writes to it serialised:

```python
from orchestrator.contracts import find_refs
from orchestrator.tools.base import RESOURCE_PENDING

    def resource_for(self, operation, args):
        repo = args.get("repo")
        if isinstance(repo, str):
            return f"github:{repo.lower()}"
        return RESOURCE_PENDING if find_refs(repo) else None
```

Derive the string only from `args`, normalise it (case, `..` segments), and return
`RESOURCE_PENDING` when the value is still a `$ref` to another task's output — the final check then
happens at execution time with the real value.

## 4. Optional: health checks and cleanup

```python
    async def health_check(self) -> bool:   # False or an exception → unhealthy → not routed
        return await self._client.ping()

    async def aclose(self) -> None:         # called on unregister / shutdown
        await self._client.close()
```

## 5. Register it and grant access

```python
await registry.register(CurrencyTool())                      # at startup or any time later
await registry.register(CurrencyTool(), replace_existing=True)  # hot-replace an implementation
```

In a deployment, register tools in your startup code alongside `register_default_tools`. Then
grant access in the policy file — registering a tool authorizes no one:

```json
{"subject": "role:finance", "tool_id": "currency", "operation": "convert", "max_access": "read"}
```

## 6. Test it

The patterns in `tests/` apply directly: call `await tool.invoke("convert", args, ctx)` for unit
tests; build an `ExecutionPlan` and run it through `DagScheduler` to test retries, timeouts and
permissions; and run the whole flow through `Orchestrator.submit` as the examples do.
