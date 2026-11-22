"""File Processing tool.

All paths are resolved inside a single root directory (``Settings.file_tool_root``). Absolute
paths, ``..`` traversal and symlinks pointing outside the root are rejected. Reads are size-
capped. The tool is read-only by design: it declares no write operations.
"""

from __future__ import annotations

import asyncio
import csv
import fnmatch
import io
import posixpath
from pathlib import Path
from typing import Any

from orchestrator.contracts import (
    AccessLevel,
    ExecutionConstraints,
    ExecutionContext,
    OperationSpec,
    ToolSpec,
    find_refs,
)
from orchestrator.errors import PermissionDeniedError, ToolExecutionError
from orchestrator.tools.base import RESOURCE_PENDING, ToolAdapter

DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def resolve_inside(root: Path, relative: str) -> Path:
    """Map a user-supplied relative path to a location inside ``root``, or raise
    ``PermissionDeniedError``. Rejects absolute paths, ``..``, hidden segments (``.name``, which
    also keeps the upload staging area unreachable) and symlinks that point outside the root."""
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise PermissionDeniedError("path must be relative to the upload directory", details={"path": relative})
    if any(part.startswith(".") for part in candidate.parts):
        raise PermissionDeniedError("hidden files and folders are not allowed", details={"path": relative})
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):  # catches symlink escapes
        raise PermissionDeniedError("path escapes the upload directory", details={"path": relative})
    return resolved


def file_resource(relative: str) -> str:
    """The permission resource for a path: ``file:<normalised relative path>``. Normalising
    (``a/../b`` -> ``b``) matters because grants are matched against this string."""
    return "file:" + posixpath.normpath(relative.replace("\\", "/"))


def is_hidden(relative_posix: str) -> bool:
    return any(part.startswith(".") for part in relative_posix.split("/"))
DEFAULT_MAX_ROWS = 10_000

_PATH = {"type": "string", "minLength": 1, "maxLength": 512}

SPEC = ToolSpec(
    tool_id="file_processing",
    name="File Processing",
    description="Reads uploaded text and CSV files from the sandboxed upload directory.",
    reliability=0.9,
    constraints=ExecutionConstraints(timeout_s=15, max_retries=0, expected_latency_ms=50),
    operations=(
        OperationSpec(
            name="list_files",
            description="List uploaded files, optionally filtered by a glob pattern such as '*.csv'.",
            capabilities=("file.list",),
            input_schema={
                "type": "object",
                "properties": {"pattern": {"type": "string", "maxLength": 128}},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"files": {"type": "array", "items": {"type": "string"}}},
                "required": ["files"],
            },
            access=AccessLevel.READ,
        ),
        OperationSpec(
            name="read_text",
            description="Read an uploaded text file (UTF-8).",
            capabilities=("file.read.text",),
            input_schema={
                "type": "object",
                "properties": {"path": _PATH},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                },
                "required": ["path", "content", "size_bytes"],
            },
            access=AccessLevel.READ,
        ),
        OperationSpec(
            name="read_csv",
            description="Parse an uploaded CSV file into a header list and row objects. Every cell is a string, including numbers; convert them before doing arithmetic.",
            capabilities=("file.read.csv", "data.tabular.load"),
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "max_rows": {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_ROWS},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "object"}},
                    "row_count": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                },
                "required": ["path", "columns", "rows", "row_count", "truncated"],
            },
            access=AccessLevel.READ,
        ),
    ),
)


class FileProcessingTool(ToolAdapter):
    spec = SPEC

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._root = Path(root).resolve()
        self._max_bytes = max_bytes

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, relative: str) -> Path:
        """Map a user-supplied path to a file inside the root, or raise."""
        return resolve_inside(self._root, relative)

    def _read_bytes(self, relative: str) -> tuple[Path, bytes]:
        path = self.resolve(relative)
        if not path.is_file():
            raise ToolExecutionError(f"file not found: {relative}", details={"path": relative})
        size = path.stat().st_size
        if size > self._max_bytes:
            raise ToolExecutionError(
                f"file exceeds {self._max_bytes} bytes", details={"path": relative, "size": size}
            )
        return path, path.read_bytes()

    @staticmethod
    def _decode(data: bytes, relative: str) -> str:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(f"{relative} is not valid UTF-8", details={"path": relative}) from exc

    async def op_list_files(self, args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        pattern = args.get("pattern") or "*"

        def _list() -> list[str]:
            if not self._root.is_dir():
                return []
            out = []
            for p in self._root.rglob("*"):
                if p.is_file() and p.resolve().is_relative_to(self._root):
                    rel = p.relative_to(self._root).as_posix()
                    if is_hidden(rel):
                        continue
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern):
                        out.append(rel)
            return sorted(out)

        return {"files": await asyncio.to_thread(_list)}

    async def op_read_text(self, args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        rel = args["path"]
        _, data = await asyncio.to_thread(self._read_bytes, rel)
        return {"path": rel, "content": self._decode(data, rel), "size_bytes": len(data)}

    async def op_read_csv(self, args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        rel = args["path"]
        max_rows = args.get("max_rows", DEFAULT_MAX_ROWS)
        _, data = await asyncio.to_thread(self._read_bytes, rel)
        reader = csv.DictReader(io.StringIO(self._decode(data, rel)))
        if not reader.fieldnames:
            raise ToolExecutionError(f"{rel} has no CSV header row", details={"path": rel})
        rows: list[dict[str, Any]] = []
        truncated = False
        try:
            for row in reader:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                # Extra cells land under the None key; drop them rather than emit a null key.
                rows.append({k: v for k, v in row.items() if k is not None})
        except csv.Error as exc:
            raise ToolExecutionError(f"malformed CSV in {rel}: {exc}", details={"path": rel}) from exc
        return {
            "path": rel,
            "columns": list(reader.fieldnames),
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }

    def resource_for(self, operation: str, args: dict[str, Any]) -> str | None | object:
        """``file:<normalised relative path>``; ``file:`` for directory listings.

        Normalising (``a/../b`` -> ``b``) matters: permission patterns are matched against this
        string, so ``reports/../secret.csv`` must not look like it is under ``reports/``.
        """
        if operation == "list_files":
            return "file:"
        path = args.get("path")
        if isinstance(path, str):
            return file_resource(path)
        if find_refs(path):
            return RESOURCE_PENDING
        return None

    def peek_columns(self, relative: str, max_bytes: int = 64 * 1024) -> list[str] | None:
        """The header row of a CSV (column names only, no data), or None if it is not a readable
        CSV. Reads at most ``max_bytes``. Callers must check permission first."""
        try:
            path = self.resolve(relative)
        except PermissionDeniedError:
            return None
        if path.suffix.lower() != ".csv" or not path.is_file():
            return None
        with path.open("rb") as fh:
            head = fh.read(max_bytes)
        try:
            text = head.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
        first = next(csv.reader(io.StringIO(text)), None)
        return [c.strip() for c in first if c.strip()] if first else None

    async def health_check(self) -> bool:
        return self._root.is_dir()
