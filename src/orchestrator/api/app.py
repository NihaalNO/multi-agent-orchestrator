"""HTTP API (FastAPI).

Phase 01/02 surface: health, tool discovery, capability listing, and admin operations
(enable/disable, health checks). ``POST /requests`` arrives with the orchestrator facade in
Phase 07.

Admin endpoints require ``Authorization: Bearer <ORCH_ADMIN_TOKEN>``. If no admin token is
configured they are disabled (default deny). This is a stopgap until principal-based auth
(Phase 04).
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from orchestrator import __version__
from orchestrator.config import Settings
from orchestrator.contracts.tools import ToolDescriptor
from orchestrator.errors import (
    DuplicateToolError,
    OperationNotFoundError,
    OrchestratorError,
    SchemaValidationError,
    ToolNotFoundError,
)
from orchestrator.observability.audit import AuditLog
from orchestrator.registry.registry import ToolRegistry

_STATUS_BY_ERROR: list[tuple[type[OrchestratorError], int]] = [
    (ToolNotFoundError, 404),
    (OperationNotFoundError, 404),
    (DuplicateToolError, 409),
    (SchemaValidationError, 422),
]


def _status_for(exc: OrchestratorError) -> int:
    for cls, status in _STATUS_BY_ERROR:
        if isinstance(exc, cls):
            return status
    return 400


def _descriptor_json(d: ToolDescriptor) -> dict[str, Any]:
    return {**d.model_dump(mode="json"), "available": d.available}


def create_app(
    *,
    settings: Settings | None = None,
    registry: ToolRegistry | None = None,
    audit: AuditLog | None = None,
) -> FastAPI:
    # Explicit `is None` checks: an empty ToolRegistry is falsy (it defines __len__).
    if settings is None:
        settings = Settings.from_env()
    if audit is None:
        audit = AuditLog()
    if registry is None:
        registry = ToolRegistry(audit, health_check_timeout_s=settings.health_check_timeout_s)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await registry.aclose()

    app = FastAPI(title="Multi-Tool Orchestrator", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.audit = audit

    @app.exception_handler(OrchestratorError)
    async def _orchestrator_error(_: Request, exc: OrchestratorError) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc), content={"error": exc.to_dict()})

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        token = settings.admin_token
        if token is None:
            raise HTTPException(403, "admin API disabled: ORCH_ADMIN_TOKEN is not configured")
        expected = f"Bearer {token.get_secret_value()}"
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "invalid or missing admin token")

    admin = [Depends(require_admin)]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "tools": len(registry)}

    @app.get("/tools")
    async def list_tools(capability: str | None = None, available_only: bool = False) -> dict[str, Any]:
        if capability is not None:
            matches = registry.find_by_capability(capability, available_only=available_only)
            return {"capability": capability, "matches": [m.model_dump(mode="json") for m in matches]}
        tools = registry.list_tools(include_unavailable=not available_only)
        return {"tools": [_descriptor_json(d) for d in tools]}

    @app.get("/tools/{tool_id}")
    async def get_tool(tool_id: str) -> dict[str, Any]:
        return _descriptor_json(registry.get(tool_id).descriptor())

    @app.get("/capabilities")
    async def capabilities(available_only: bool = True) -> dict[str, list[str]]:
        return registry.capabilities(available_only=available_only)

    @app.post("/admin/tools/{tool_id}/enable", dependencies=admin)
    async def enable_tool(tool_id: str) -> dict[str, Any]:
        return _descriptor_json(await registry.set_enabled(tool_id, True))

    @app.post("/admin/tools/{tool_id}/disable", dependencies=admin)
    async def disable_tool(tool_id: str) -> dict[str, Any]:
        return _descriptor_json(await registry.set_enabled(tool_id, False))

    @app.post("/admin/tools/health-check", dependencies=admin)
    async def health_check(tool_id: str | None = None) -> dict[str, str]:
        return {k: v.value for k, v in (await registry.check_health(tool_id)).items()}

    return app
