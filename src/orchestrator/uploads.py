"""File uploads into the folder the file tool reads (``ORCH_FILE_TOOL_ROOT``).

Safety properties:

* **Permissions** come from the same default-deny policy, under a separate permission target,
  ``tool_id: "uploads"`` (operations ``upload``, ``list``, ``delete``), with the resource
  ``file:<path>``. It is *not* a registered tool, so the planner can never write or delete files;
  only an authenticated user calling the API can. ``{user_id}`` in grant patterns gives per-user
  folders (``file:users/{user_id}/*``).
* **Paths** go through the same resolver as the file tool: relative only, no ``..``, no hidden
  segments, no symlink escapes.
* **Size** is enforced while bytes stream in, so an oversized upload never fully lands on disk;
  the default cap equals the file tool's read cap, since a bigger file could not be read anyway.
* **Types**: an extension allow-list, and text types must be valid UTF-8 (what the file tool reads).
* **Atomic commit**: bytes stream to a hidden staging file, then appear under the final name in one
  step. Without ``overwrite`` the commit fails if the name exists — including when two uploads race
  for the same name — so a file is never silently replaced.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from orchestrator.contracts import AccessLevel, OperationSpec, PermissionPolicy, Principal, ToolSpec
from orchestrator.errors import OrchestratorError, PermissionDeniedError
from orchestrator.observability.audit import AuditLog
from orchestrator.tools.file_processing import file_resource, is_hidden, resolve_inside

STAGING_DIR = ".staging"  # hidden, so the file tool and the API can never address it
TEXT_TYPES = frozenset({".csv", ".txt", ".md", ".json"})

UPLOADS_SPEC = ToolSpec(
    tool_id="uploads",
    name="File uploads",
    description="Permission target for the /files API. Not a registered tool: the planner cannot use it.",
    operations=(
        OperationSpec(name="upload", description="Upload a file", capabilities=("file.upload",),
                      access=AccessLevel.WRITE, idempotent=False),
        OperationSpec(name="list", description="List uploaded files", capabilities=("file.list",),
                      access=AccessLevel.READ),
        OperationSpec(name="delete", description="Delete an uploaded file", capabilities=("file.delete",),
                      access=AccessLevel.WRITE, idempotent=False),
    ),
)


class UploadTooLargeError(OrchestratorError):
    code = "upload_too_large"


class UnsupportedFileTypeError(OrchestratorError):
    code = "unsupported_file_type"


class FileExistsConflictError(OrchestratorError):
    code = "file_exists"


class UploadedFileNotFoundError(OrchestratorError):
    code = "file_not_found"


class FileInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    size_bytes: int
    modified: datetime
    sha256: str | None = None


class UploadStore:
    def __init__(
        self,
        root: Path,
        policy: PermissionPolicy,
        audit: AuditLog | None = None,
        *,
        max_bytes: int,
        allowed_extensions: frozenset[str],
    ) -> None:
        self._root = Path(root).resolve()
        self._policy = policy
        self._audit = audit or AuditLog()
        self._max_bytes = max_bytes
        self._extensions = frozenset(e.lower() if e.startswith(".") else f".{e.lower()}" for e in allowed_extensions)

    # ------------------------------------------------------------------ public

    async def save(
        self, principal: Principal, relative: str, chunks: AsyncIterator[bytes], *, overwrite: bool = False,
        declared_size: int | None = None,
    ) -> tuple[FileInfo, bool]:
        """Store an upload. Returns (info, replaced)."""
        target = resolve_inside(self._root, relative)
        suffix = target.suffix.lower()
        if suffix not in self._extensions:
            raise UnsupportedFileTypeError(
                f"files of type {suffix or '(none)'!r} are not accepted",
                details={"allowed": sorted(self._extensions)},
            )
        resource = file_resource(relative)
        self._authorize(principal, "upload", resource)
        if declared_size is not None and declared_size > self._max_bytes:
            raise UploadTooLargeError(f"upload exceeds {self._max_bytes} bytes", details={"max_bytes": self._max_bytes})
        if target.exists() and not overwrite:
            raise FileExistsConflictError(f"{relative} already exists; pass overwrite=true to replace it")

        staging = self._root / STAGING_DIR
        staging.mkdir(parents=True, exist_ok=True)
        temp = staging / f"{uuid.uuid4().hex}.part"
        digest, size = hashlib.sha256(), 0
        replaced = False
        try:
            with temp.open("wb") as fh:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise UploadTooLargeError(
                            f"upload exceeds {self._max_bytes} bytes", details={"max_bytes": self._max_bytes}
                        )
                    digest.update(chunk)
                    fh.write(chunk)
            if suffix in TEXT_TYPES:
                try:
                    temp.read_bytes().decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise UnsupportedFileTypeError(f"{relative} is not valid UTF-8 text") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            replaced = target.exists()
            if overwrite:
                os.replace(temp, target)
            else:
                self._commit_new(temp, target, relative)
        finally:
            temp.unlink(missing_ok=True)

        info = FileInfo(path=Path(relative).as_posix(), size_bytes=size, sha256=digest.hexdigest(),
                        modified=datetime.fromtimestamp(target.stat().st_mtime, UTC))
        self._audit.record("file.uploaded", user_id=principal.user_id, path=info.path, size_bytes=size,
                           sha256=info.sha256, replaced=replaced)
        return info, replaced

    def list(self, principal: Principal, pattern: str | None = None) -> list[FileInfo]:
        """Files the caller may list (``uploads.list`` on each file's resource)."""
        import fnmatch

        if not self._root.is_dir():
            return []
        op = UPLOADS_SPEC.operation("list")
        out = []
        for p in sorted(self._root.rglob("*")):
            if not p.is_file() or not p.resolve().is_relative_to(self._root):
                continue
            rel = p.relative_to(self._root).as_posix()
            if is_hidden(rel) or (pattern and not (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern))):
                continue
            if self._policy.check(principal, UPLOADS_SPEC, op, file_resource(rel)).allowed:
                stat = p.stat()
                out.append(FileInfo(path=rel, size_bytes=stat.st_size,
                                    modified=datetime.fromtimestamp(stat.st_mtime, UTC)))
        return out

    def delete(self, principal: Principal, relative: str) -> None:
        target = resolve_inside(self._root, relative)
        self._authorize(principal, "delete", file_resource(relative))
        if not target.is_file():
            raise UploadedFileNotFoundError(f"{relative} not found")
        target.unlink()
        self._audit.record("file.deleted", user_id=principal.user_id, path=Path(relative).as_posix())

    # ------------------------------------------------------------------ internals

    def _authorize(self, principal: Principal, operation: str, resource: str) -> None:
        op = UPLOADS_SPEC.operation(operation)
        assert op is not None
        decision = self._policy.check(principal, UPLOADS_SPEC, op, resource)
        self._audit.record("permission.decision", tool_id="uploads", user_id=principal.user_id, operation=operation,
                           resource=resource, allowed=decision.allowed, reason=decision.reason)
        if not decision.allowed:
            raise PermissionDeniedError(decision.reason, details={"operation": operation, "resource": resource})

    @staticmethod
    def _commit_new(temp: Path, target: Path, relative: str) -> None:
        """Make ``temp`` appear as ``target`` only if ``target`` does not exist — atomically, so
        two racing uploads cannot both win."""
        try:
            os.link(temp, target)  # fails with FileExistsError if the name is taken
            return
        except FileExistsError:
            raise FileExistsConflictError(f"{relative} already exists; pass overwrite=true to replace it") from None
        except OSError:
            pass  # filesystem without hard links: fall back to an exclusive create
        try:
            with open(target, "xb") as dst, temp.open("rb") as src:
                shutil.copyfileobj(src, dst)
        except FileExistsError:
            raise FileExistsConflictError(f"{relative} already exists; pass overwrite=true to replace it") from None
