"""Live progress for one request, built on the audit events the orchestrator already records.

``ProgressHub`` is an audit sink. A client that started a request subscribes to that request's id
and receives a small, whitelisted summary of each event as it happens: the plan's task names,
each step starting and finishing (tool, status, duration), retries, re-plans, conflicts, the answer
being written, the end. Event data beyond those fields — arguments, outputs, file paths, user ids —
is never forwarded: progress is shown to the requester while the request runs, and the full record
(with its own access checks) is what carries results.

Subscriptions exist only while a stream is open. Request ids are random and created by the server
before the request starts, so a stream can only observe the request it started.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from orchestrator.observability.audit import AuditEvent

_QUEUE_LIMIT = 500  # a slow client loses the oldest progress lines, never blocks execution


def to_progress(event: AuditEvent) -> dict[str, Any] | None:
    """The client-visible form of an audit event, or None if it is not progress."""
    d, task = event.data, event.task_id
    k = event.kind
    if k == "request.received":
        return {"type": "received"}
    if k == "planner.retry":
        return {"type": "planner_retry"}
    if k == "planner.proposal":
        return {"type": "planned", "tasks": list(d.get("tasks") or []), "unsupported": len(d.get("unsupported") or [])}
    if k == "execution.started":
        return {"type": "executing", "tasks": list(d.get("tasks") or [])}
    if k == "task.started":
        return {"type": "task_started", "task_id": task, "tool": event.tool_id, "operation": d.get("operation")}
    if k == "task.retry":
        return {"type": "task_retry", "task_id": task, "attempt": d.get("attempt"), "error": d.get("error")}
    if k == "task.finished":
        return {"type": "task_finished", "task_id": task, "tool": event.tool_id, "status": d.get("status"),
                "duration_ms": d.get("duration_ms"), "deduplicated": bool(d.get("deduplicated")),
                "error": d.get("error")}
    if k == "approval.requested":
        return {"type": "approval_needed", "task_id": task}
    if k == "request.replanning":
        return {"type": "replanning", "attempt": d.get("attempt"), "failed": sorted((d.get("failed") or {}).keys())}
    if k in ("conflict.detected", "conflict.resolved", "conflict.escalated"):
        return {"type": "conflict", "kind": d.get("conflict_kind"), "outcome": k.split(".", 1)[1]}
    if k == "code.writing":
        return {"type": "coding"}
    if k == "code.written":
        return {"type": "coded", "language": d.get("language"), "runnable": bool(d.get("runnable"))}
    if k == "answer.started":
        return {"type": "answering"}
    if k == "answer.composed":
        return {"type": "answered", "claims": d.get("claims"), "supported": d.get("supported")}
    if k == "answer.failed":
        return {"type": "answer_failed"}
    if k == "request.finished":
        return {"type": "finished", "state": d.get("state")}
    return None


class Subscription:
    def __init__(self, request_id: str, loop: asyncio.AbstractEventLoop, unsubscribe: Callable[["Subscription"], None]) -> None:
        self.request_id = request_id
        self._loop = loop
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._unsubscribe = unsubscribe

    def _offer(self, message: dict[str, Any]) -> None:  # runs on the subscriber's loop
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(message)

    def push(self, message: dict[str, Any]) -> None:
        self._loop.call_soon_threadsafe(self._offer, message)

    async def get(self) -> dict[str, Any]:
        return await self._queue.get()

    def drain(self) -> list[dict[str, Any]]:
        items = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        return items

    def close(self) -> None:
        self._unsubscribe(self)


class ProgressHub:
    """Audit sink fanning progress out to subscribers of a request id."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Subscription]] = {}
        self._lock = threading.Lock()

    def subscribe(self, request_id: str) -> Subscription:
        sub = Subscription(request_id, asyncio.get_running_loop(), self._remove)
        with self._lock:
            self._subs.setdefault(request_id, []).append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            subs = self._subs.get(sub.request_id, [])
            if sub in subs:
                subs.remove(sub)
            if not subs:
                self._subs.pop(sub.request_id, None)

    def subscribers(self, request_id: str) -> int:
        with self._lock:
            return len(self._subs.get(request_id, []))

    def emit(self, event: AuditEvent) -> None:
        if event.request_id is None:
            return
        with self._lock:
            subs = list(self._subs.get(event.request_id, ()))
        if not subs:
            return
        message = to_progress(event)
        if message is None:
            return
        message["at"] = event.timestamp.isoformat()
        for sub in subs:
            sub.push(dict(message))
