"""Tool adapter base class.

To add a tool: subclass ``ToolAdapter``, set ``spec``, and implement one coroutine named
``op_<operation_name>`` per declared operation. The registry rejects adapters whose declared
operations have no handler. No orchestrator code changes are needed.

Adapters should raise ``TransientToolError`` for failures worth retrying and
``ToolExecutionError`` for everything else. Unexpected exceptions are treated as
non-recoverable by the engine.
"""

from __future__ import annotations

import inspect
from abc import ABC
from typing import Any, Awaitable, Callable, ClassVar

from orchestrator.contracts.context import ExecutionContext
from orchestrator.contracts.tools import ToolSpec
from orchestrator.errors import OperationNotFoundError

OperationHandler = Callable[[dict[str, Any], ExecutionContext], Awaitable[dict[str, Any]]]

HANDLER_PREFIX = "op_"


class ToolAdapter(ABC):
    spec: ClassVar[ToolSpec]

    @property
    def tool_id(self) -> str:
        return self.spec.tool_id

    def handler(self, operation: str) -> OperationHandler:
        if self.spec.operation(operation) is None:
            raise OperationNotFoundError(
                f"tool {self.tool_id!r} does not declare operation {operation!r}"
            )
        fn = getattr(self, f"{HANDLER_PREFIX}{operation}", None)
        if fn is None or not inspect.iscoroutinefunction(fn):
            raise OperationNotFoundError(
                f"tool {self.tool_id!r} has no async handler for operation {operation!r}"
            )
        return fn

    def missing_handlers(self) -> list[str]:
        missing = []
        for op in self.spec.operations:
            fn = getattr(self, f"{HANDLER_PREFIX}{op.name}", None)
            if fn is None or not inspect.iscoroutinefunction(fn):
                missing.append(op.name)
        return missing

    async def invoke(
        self, operation: str, args: dict[str, Any], ctx: ExecutionContext
    ) -> dict[str, Any]:
        """Call the handler directly. The execution engine wraps this with validation,
        authorization, timeouts and retries — do not call it from orchestration code."""
        return await self.handler(operation)(args, ctx)

    async def health_check(self) -> bool:
        """Override for tools with external dependencies. Default: healthy."""
        return True

    async def aclose(self) -> None:
        """Release resources (connections, subprocesses). Called on unregister."""
        return None
