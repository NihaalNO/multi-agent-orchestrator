"""HTTP API (FastAPI).

Every endpoint except ``/health`` and the chat UI (``/ui``, static files) requires ``Authorization: Bearer <api key>``; the key maps to
a ``Principal`` (see ``api/auth.py``). Admin endpoints additionally require the admin role,
approval decisions the approver role.

Files: PUT /files/{path} (raw body), GET /files, DELETE /files/{path} — see ``orchestrator.uploads``.

Request lifecycle over HTTP:
  POST /requests                 plan, route, authorize, execute, resolve; returns the record
  POST /requests/stream          the same, as server-sent events: live progress, then the record
  POST /requests/{id}/replay     run the stored plan again and compare every step with the original
  GET  /requests/{id}            the record (owner or admin)
  POST /requests/{id}/resume     after approvals are decided, run what is left
Status codes: 200 completed/failed, 202 awaiting approval, 4xx/5xx rejected (body is the record).
"""

# No `from __future__ import annotations` here: FastAPI must resolve the closure-local
# dependency aliases (PrincipalDep, AdminDep, ...) at route-definition time.
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from orchestrator.core import Orchestrator, RequestRecord, RequestState, register_default_tools
from orchestrator.observability import InMemoryAuditSink, JsonlAuditSink, LoggingAuditSink, MetricsSink

from orchestrator import __version__
from orchestrator.api.auth import ApiKeyAuthenticator
from orchestrator.config import Settings
from orchestrator.contracts import ApprovalStatus, Principal, ToolDescriptor
from orchestrator.errors import (
    ApprovalRequiredError,
    ConfigurationError,
    DuplicateToolError,
    InvalidRequestStateError,
    PlannerUnavailableError,
    RequestNotFoundError,
    NoMatchingToolError,
    OperationNotFoundError,
    OrchestratorError,
    PermissionDeniedError,
    SchemaValidationError,
    ToolNotFoundError,
)
from orchestrator.observability.audit import AuditLog
from orchestrator.observability.progress import ProgressHub
from orchestrator.permissions import (
    ApprovalManager,
    ApprovalNotFoundError,
    GrantPolicy,
    PermissionManager,
    load_grants,
    load_principals,
)
from orchestrator.registry.registry import ToolRegistry
from orchestrator.uploads import (
    FileExistsConflictError,
    UnsupportedFileTypeError,
    UploadedFileNotFoundError,
    UploadStore,
    UploadTooLargeError,
)

_STATUS_BY_ERROR: list[tuple[type[OrchestratorError], int]] = [
    (ToolNotFoundError, 404),
    (OperationNotFoundError, 404),
    (ApprovalNotFoundError, 404),
    (UploadedFileNotFoundError, 404),
    (UploadTooLargeError, 413),
    (UnsupportedFileTypeError, 415),
    (FileExistsConflictError, 409),
    (RequestNotFoundError, 404),
    (PermissionDeniedError, 403),
    (DuplicateToolError, 409),
    (ApprovalRequiredError, 409),
    (InvalidRequestStateError, 409),
    (SchemaValidationError, 422),
    (NoMatchingToolError, 422),
    (PlannerUnavailableError, 503),
    (ConfigurationError, 503),
]
_STATUS_BY_CODE = {cls.code: status for cls, status in _STATUS_BY_ERROR}


def _status_for(exc: OrchestratorError) -> int:
    for cls, status in _STATUS_BY_ERROR:
        if isinstance(exc, cls):
            return status
    return 400


# Chat UI: static files, read on each request (they are small). The page holds no data and no
# secrets; it calls the same authenticated endpoints as any client, with the key the user enters.
_UI_DIR = Path(__file__).parent / "static"
_UI_TYPES = {
    "chat.html": "text/html; charset=utf-8",
    "chat.css": "text/css; charset=utf-8",
    "chat.js": "text/javascript; charset=utf-8",
}
_UI_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


# Self-hosted fonts (SIL OFL; licenses beside them). Only these names are served.
_UI_FONTS = {"public-sans-400.woff2", "public-sans-600.woff2", "public-sans-700.woff2", "barlow-condensed-600.woff2"}


def _ui_font(name: str) -> Response:
    if name not in _UI_FONTS:
        raise HTTPException(404, "not found")
    return Response((_UI_DIR / "fonts" / name).read_bytes(), media_type="font/woff2",
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=31536000, immutable"})


def _ui_file(name: str) -> Response:
    if name not in _UI_TYPES:
        raise HTTPException(404, "not found")
    return Response((_UI_DIR / name).read_bytes(), media_type=_UI_TYPES[name], headers=_UI_HEADERS)


def _descriptor_json(d: ToolDescriptor) -> dict[str, Any]:
    return {**d.model_dump(mode="json"), "available": d.available}


class DecisionBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class SubmitBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    files: list[str] = Field(default_factory=list, max_length=50)
    allow_partial: bool = False
    # Earlier requests of this conversation (your own, completed), oldest first: follow-ups can use
    # their results ("now double it"). See Orchestrator.submit.
    context: list[str] = Field(default_factory=list, max_length=5)
    # "auto": code answers for requests that ask for code; "code": always; "task": never.
    mode: Literal["auto", "code", "task"] = "auto"


def _record_response(record: RequestRecord) -> JSONResponse:
    if record.state == RequestState.REJECTED:
        status = _STATUS_BY_CODE.get(record.error.code if record.error else "", 422)
    elif record.state == RequestState.AWAITING_APPROVAL:
        status = 202
    else:
        status = 200
    return JSONResponse(status_code=status, content=record.public())


def create_app(
    *,
    settings: Settings | None = None,
    registry: ToolRegistry | None = None,
    audit: AuditLog | None = None,
    authenticator: ApiKeyAuthenticator | None = None,
    permissions: PermissionManager | None = None,
    approvals: ApprovalManager | None = None,
    orchestrator: Orchestrator | None = None,
    uploads: UploadStore | None = None,
    metrics: MetricsSink | None = None,
    register_defaults: bool | None = None,
) -> FastAPI:
    """Everything is injectable for tests. Defaults come from ``Settings``. Built-in tools are
    registered at startup when the app creates its own registry (``register_defaults``)."""
    # Explicit `is None` checks: an empty ToolRegistry is falsy (it defines __len__).
    if settings is None:
        settings = Settings.from_env()
    if metrics is None:
        metrics = MetricsSink()
        if audit is not None:
            audit.add_sink(metrics)
    if audit is None:
        sinks = [InMemoryAuditSink(), metrics]
        if settings.audit_log_path:
            sinks.append(JsonlAuditSink(settings.audit_log_path))
        if settings.audit_to_log:
            sinks.append(LoggingAuditSink())
        audit = AuditLog(*sinks)
    if register_defaults is None:
        register_defaults = registry is None
    if registry is None:
        registry = ToolRegistry(audit, health_check_timeout_s=settings.health_check_timeout_s)
    if authenticator is None:
        records = load_principals(settings.principals_path) if settings.principals_path else []
        authenticator = ApiKeyAuthenticator(records)
    if permissions is None:
        grants = load_grants(settings.policy_path) if settings.policy_path else []
        permissions = PermissionManager(GrantPolicy(grants), registry, audit)
    # Durable state only when the app builds its own orchestrator; injected components keep theirs.
    state = None
    if settings.state_path and orchestrator is None:
        from orchestrator.state import open_state

        state = open_state(settings.state_path)
        if state.recovered:
            audit.record("state.recovered", interrupted_requests=state.recovered)
    if approvals is None:
        approvals = ApprovalManager(
            audit, approver_role=settings.approver_role, ttl=timedelta(seconds=settings.approval_ttl_s),
            store=state.approvals if state else None,
        )

    if uploads is None:
        uploads = UploadStore(
            settings.file_tool_root, permissions.policy, audit,
            max_bytes=settings.upload_max_bytes, allowed_extensions=settings.upload_extension_set,
        )
    if orchestrator is None:
        orchestrator = Orchestrator.build(
            settings, registry, permissions, approvals, audit,
            store=state.requests if state else None,
            idempotency_backend=state.idempotency if state else None,
            receipt_chain=state.receipts if state else None,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if register_defaults:
            await register_default_tools(registry, settings)
        try:
            yield
        finally:
            await registry.aclose()
            if state is not None:
                state.close()

    progress = ProgressHub()
    orchestrator.audit.add_sink(progress)
    background: set[asyncio.Task] = set()  # requests whose stream closed early keep running

    app = FastAPI(title="Multi-Tool Orchestrator", version=__version__, lifespan=lifespan)
    app.state.progress = progress
    app.state.settings = settings
    app.state.registry = registry
    app.state.audit = audit
    app.state.permissions = permissions
    app.state.approvals = approvals
    app.state.orchestrator = orchestrator
    app.state.metrics = metrics

    @app.exception_handler(OrchestratorError)
    async def _orchestrator_error(_: Request, exc: OrchestratorError) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc), content={"error": exc.to_dict()})

    # ------------------------------------------------------------------ auth dependencies

    def current_principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
        principal = authenticator.authenticate(authorization)
        if principal is None:
            raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
        return principal

    PrincipalDep = Annotated[Principal, Depends(current_principal)]

    def require_role(role: str):
        def dep(principal: PrincipalDep) -> Principal:
            if role not in principal.roles:
                audit.record("auth.forbidden", user_id=principal.user_id, required_role=role)
                raise HTTPException(403, f"requires the {role!r} role")
            return principal

        return Depends(dep)

    AdminDep = Annotated[Principal, require_role(settings.admin_role)]
    ApproverDep = Annotated[Principal, require_role(settings.approver_role)]

    # ------------------------------------------------------------------ public

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/ui")

    @app.get("/ui", include_in_schema=False)
    async def chat_page() -> Response:
        return _ui_file("chat.html")

    @app.get("/ui/fonts/{name}", include_in_schema=False)
    async def chat_font(name: str) -> Response:
        return _ui_font(name)

    @app.get("/ui/{name}", include_in_schema=False)
    async def chat_asset(name: str) -> Response:
        if name == "chat.html":
            raise HTTPException(404, "not found")
        return _ui_file(name)

    # ------------------------------------------------------------------ authenticated

    @app.get("/me")
    async def me(principal: PrincipalDep) -> dict[str, Any]:
        return {"user_id": principal.user_id, "roles": sorted(principal.roles)}

    @app.get("/tools")
    async def list_tools(
        principal: PrincipalDep, capability: str | None = None, available_only: bool = False
    ) -> dict[str, Any]:
        if capability is not None:
            matches = registry.find_by_capability(capability, available_only=available_only)
            return {"capability": capability, "matches": [m.model_dump(mode="json") for m in matches]}
        tools = registry.list_tools(include_unavailable=not available_only)
        return {"tools": [_descriptor_json(d) for d in tools]}

    @app.get("/tools/{tool_id}")
    async def get_tool(tool_id: str, principal: PrincipalDep) -> dict[str, Any]:
        return _descriptor_json(registry.get(tool_id).descriptor())

    @app.get("/capabilities")
    async def capabilities(principal: PrincipalDep, available_only: bool = True) -> dict[str, list[str]]:
        return registry.capabilities(available_only=available_only)

    # ------------------------------------------------------------------ requests

    @app.post("/requests")
    async def submit_request(
        body: SubmitBody,
        principal: PrincipalDep,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> JSONResponse:
        record = await orchestrator.submit(
            body.text, principal, files=tuple(body.files),
            idempotency_key=idempotency_key, allow_partial=body.allow_partial, context=tuple(body.context),
            mode=body.mode,
        )
        return _record_response(record)

    @app.post("/requests/stream")
    async def submit_request_stream(
        body: SubmitBody,
        principal: PrincipalDep,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> StreamingResponse:
        """Server-sent events: ``accepted`` (the request id), ``progress`` (one per step change,
        see ``observability.progress``), then ``record`` (the same body as ``POST /requests``) or
        ``error``. Closing the stream does not cancel the request; fetch it later by id."""
        request_id = uuid.uuid4().hex
        sub = progress.subscribe(request_id)
        task = asyncio.create_task(orchestrator.submit(
            body.text, principal, files=tuple(body.files), idempotency_key=idempotency_key,
            allow_partial=body.allow_partial, request_id=request_id, context=tuple(body.context),
            mode=body.mode,
        ))
        background.add(task)
        task.add_done_callback(background.discard)

        def sse(event: str, data: Any) -> str:
            return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        async def events():
            try:
                yield sse("accepted", {"request_id": request_id})
                while not task.done():
                    getter = asyncio.ensure_future(sub.get())
                    done, _ = await asyncio.wait({getter, task}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        yield sse("progress", getter.result())
                    else:
                        getter.cancel()
                        if not done:
                            yield ": keep-alive\n\n"
                for message in sub.drain():
                    yield sse("progress", message)
                try:
                    record = task.result()
                except OrchestratorError as exc:
                    yield sse("error", {"status": _status_for(exc), "error": exc.to_dict()})
                    return
                except Exception:
                    yield sse("error", {"status": 500, "error": {"code": "internal_error",
                                                                  "message": "the request failed unexpectedly"}})
                    return
                response = _record_response(record)
                yield sse("record", {"status": response.status_code, "record": json.loads(response.body)})
            finally:
                sub.close()

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/requests/{request_id}/replay")
    async def replay_request(request_id: str, principal: PrincipalDep) -> dict[str, Any]:
        """Run the stored plan again (read and compute steps only, under your current permissions)
        and compare each step with the original. The original record is not changed."""
        return (await orchestrator.replay(request_id, principal)).model_dump(mode="json")

    @app.get("/requests/{request_id}/receipt")
    async def get_receipt(request_id: str, principal: PrincipalDep) -> dict[str, Any]:
        record = orchestrator.get(request_id, principal)  # owner or admin; others get 404
        if record.receipt is None:
            raise RequestNotFoundError(f"request {request_id!r} has no receipt yet")
        return record.receipt.model_dump(mode="json")

    @app.get("/receipts/public-key")
    async def receipt_public_key() -> dict[str, Any]:
        """Public: the key receipts are signed with, for offline verification."""
        key = orchestrator.receipts.public_key if orchestrator.receipts else None
        if key is None:
            raise HTTPException(404, "receipts are not signed on this server")
        return {"algorithm": "Ed25519", "key_id": key[1], "public_key_pem": key[0]}

    @app.get("/admin/receipts")
    async def list_receipts(principal: AdminDep, since: int = 0, limit: int = 500) -> dict[str, Any]:
        """The receipt chain from ``since`` (exclusive), for audits: verify it with
        ``python -m orchestrator verify-receipt --chain``."""
        if orchestrator.receipts is None:
            return {"receipts": []}
        items = orchestrator.receipts.chain.since(since, min(max(limit, 1), 1000))
        return {"receipts": [r.model_dump(mode="json") for r in items]}

    @app.get("/requests/{request_id}")
    async def get_request(request_id: str, principal: PrincipalDep) -> dict[str, Any]:
        return orchestrator.get(request_id, principal).public()

    @app.post("/requests/{request_id}/resume")
    async def resume_request(request_id: str, principal: PrincipalDep) -> JSONResponse:
        return _record_response(await orchestrator.resume(request_id, principal))

    # ------------------------------------------------------------------ files

    @app.put("/files/{path:path}")
    async def upload_file(path: str, request: Request, principal: PrincipalDep, overwrite: bool = False) -> JSONResponse:
        length = request.headers.get("content-length")
        info, replaced = await uploads.save(
            principal, path, request.stream(), overwrite=overwrite,
            declared_size=int(length) if length and length.isdigit() else None,
        )
        return JSONResponse(status_code=200 if replaced else 201, content=info.model_dump(mode="json"))

    @app.get("/files")
    async def list_files(principal: PrincipalDep, pattern: str | None = None) -> dict[str, Any]:
        return {"files": [f.model_dump(mode="json") for f in uploads.list(principal, pattern)]}

    @app.delete("/files/{path:path}")
    async def delete_file(path: str, principal: PrincipalDep) -> dict[str, str]:
        uploads.delete(principal, path)
        return {"deleted": path}

    # ------------------------------------------------------------------ approvals

    @app.get("/approvals")
    async def list_approvals(principal: ApproverDep, status: ApprovalStatus | None = ApprovalStatus.PENDING) -> dict[str, Any]:
        return {"approvals": [a.model_dump(mode="json") for a in approvals.list(status)]}

    @app.get("/approvals/{approval_id}")
    async def get_approval(approval_id: str, principal: PrincipalDep) -> dict[str, Any]:
        item = approvals.get(approval_id)
        if item.requested_by != principal.user_id and settings.approver_role not in principal.roles:
            raise ApprovalNotFoundError(f"approval {approval_id!r} not found")  # don't leak existence
        return item.model_dump(mode="json")

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, principal: ApproverDep, body: DecisionBody | None = None) -> dict[str, Any]:
        item = await approvals.decide(approval_id, approver=principal, approve=True, note=body.note if body else None)
        return item.model_dump(mode="json")

    @app.post("/approvals/{approval_id}/deny")
    async def deny(approval_id: str, principal: ApproverDep, body: DecisionBody | None = None) -> dict[str, Any]:
        item = await approvals.decide(approval_id, approver=principal, approve=False, note=body.note if body else None)
        return item.model_dump(mode="json")

    # ------------------------------------------------------------------ admin

    @app.post("/admin/tools/{tool_id}/enable")
    async def enable_tool(tool_id: str, principal: AdminDep) -> dict[str, Any]:
        audit.record("admin.tool_enable", tool_id=tool_id, user_id=principal.user_id)
        return _descriptor_json(await registry.set_enabled(tool_id, True))

    @app.post("/admin/tools/{tool_id}/disable")
    async def disable_tool(tool_id: str, principal: AdminDep) -> dict[str, Any]:
        audit.record("admin.tool_disable", tool_id=tool_id, user_id=principal.user_id)
        return _descriptor_json(await registry.set_enabled(tool_id, False))

    @app.get("/admin/metrics")
    async def get_metrics(principal: AdminDep) -> dict[str, Any]:
        return metrics.snapshot()

    @app.post("/admin/tools/health-check")
    async def health_check(principal: AdminDep, tool_id: str | None = None) -> dict[str, str]:
        return {k: v.value for k, v in (await registry.check_health(tool_id)).items()}

    return app
