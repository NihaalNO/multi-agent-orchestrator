"""Structured audit log.

Every significant decision (registration, permission decision, task start/finish, retry,
conflict resolution) is recorded as an ``AuditEvent``. Values under secret-looking keys are
redacted before the event leaves this module, so sinks never see them.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|credential|private[_-]?key|cookie)",
    re.IGNORECASE,
)
_MAX_DEPTH = 8


def redact(value: Any, _depth: int = 0) -> Any:
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: str
    request_id: str | None = None
    execution_id: str | None = None
    task_id: str | None = None
    tool_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    def __init__(self, max_events: int = 10_000) -> None:
        self._events: list[AuditEvent] = []
        self._max = max_events
        self._lock = threading.Lock()

    def emit(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max:
                del self._events[: len(self._events) - self._max]

    def events(self, kind: str | None = None) -> list[AuditEvent]:
        with self._lock:
            return [e for e in self._events if kind is None or e.kind == kind]


class JsonlAuditSink:
    """Appends one JSON object per line. Suitable for local runs and log shipping."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: AuditEvent) -> None:
        line = event.model_dump_json()
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class LoggingAuditSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("orchestrator.audit")

    def emit(self, event: AuditEvent) -> None:
        self._logger.info(json.dumps(event.model_dump(mode="json")))


class AuditLog:
    def __init__(self, *sinks: AuditSink) -> None:
        self._sinks: list[AuditSink] = list(sinks) or [InMemoryAuditSink()]
        self._log = logging.getLogger(__name__)

    @property
    def sinks(self) -> list[AuditSink]:
        return list(self._sinks)

    def add_sink(self, sink: AuditSink) -> None:
        self._sinks.append(sink)

    def record(
        self,
        kind: str,
        *,
        request_id: str | None = None,
        execution_id: str | None = None,
        task_id: str | None = None,
        tool_id: str | None = None,
        **data: Any,
    ) -> AuditEvent:
        event = AuditEvent(
            kind=kind,
            request_id=request_id,
            execution_id=execution_id,
            task_id=task_id,
            tool_id=tool_id,
            data=redact(data),
        )
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # an audit sink must never break execution
                self._log.exception("audit sink %r failed", sink)
        return event
