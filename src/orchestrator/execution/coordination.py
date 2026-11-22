"""Duplicate-execution protection and per-resource write locks.

``IdempotencyStore``
    For non-idempotent operations, a call identified by an idempotency key runs at most once
    within the TTL. A concurrent duplicate waits for the in-flight call and receives its
    result; a later duplicate receives the cached result. Only successful results are cached,
    so a failed call can be retried with the same key.

``ResourceLocks``
    Serialises non-idempotent calls touching the same resource (e.g. two writes to
    ``db:records/5``) so they cannot interleave. Locks are created on demand and dropped when
    no one holds or waits for them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

from orchestrator.contracts import TaskResult, TaskStatus


class IdempotencyBackend(Protocol):
    """Where completed results are kept. Expiry times are wall-clock epoch seconds, so they stay
    meaningful across restarts when the backend is persistent."""

    def get(self, key: str, now: float) -> TaskResult | None: ...
    def put(self, key: str, expires_at: float, result: TaskResult) -> None: ...
    def evict(self, now: float) -> None: ...


class InMemoryIdempotencyBackend:
    def __init__(self) -> None:
        self._done: dict[str, tuple[float, TaskResult]] = {}

    def get(self, key: str, now: float) -> TaskResult | None:
        entry = self._done.get(key)
        return entry[1] if entry and entry[0] > now else None

    def put(self, key: str, expires_at: float, result: TaskResult) -> None:
        self._done[key] = (expires_at, result)

    def evict(self, now: float) -> None:
        for k in [k for k, (exp, _) in self._done.items() if exp <= now]:
            del self._done[k]


class IdempotencyStore:
    """In-flight calls are coordinated in memory (they cannot outlive the process anyway);
    completed results live in the backend, which may be persistent."""

    def __init__(
        self,
        ttl_s: float = 24 * 3600,
        *,
        clock: Callable[[], float] = time.time,
        backend: IdempotencyBackend | None = None,
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._backend: IdempotencyBackend = backend if backend is not None else InMemoryIdempotencyBackend()
        self._inflight: dict[str, asyncio.Future[TaskResult]] = {}
        self._lock = asyncio.Lock()

    async def run_once(self, key: str, fn: Callable[[], Awaitable[TaskResult]]) -> TaskResult:
        while True:
            async with self._lock:
                now = self._clock()
                self._backend.evict(now)
                done = self._backend.get(key, now)
                if done is not None:
                    return done.model_copy(update={"deduplicated": True})
                waiting = self._inflight.get(key)
                if waiting is None:
                    future: asyncio.Future[TaskResult] = asyncio.get_running_loop().create_future()
                    self._inflight[key] = future
                    break
            try:
                result = await asyncio.shield(waiting)
            except asyncio.CancelledError:
                if waiting.cancelled():
                    continue  # the original call was cancelled: try to run it ourselves
                raise
            return result.model_copy(update={"deduplicated": True})

        try:
            result = await fn()
        except BaseException:
            async with self._lock:
                self._inflight.pop(key, None)
            future.cancel()
            raise
        async with self._lock:
            self._inflight.pop(key, None)
            if result.status == TaskStatus.SUCCEEDED:
                self._backend.put(key, self._clock() + self._ttl, result)
        future.set_result(result)
        return result

class ResourceLocks:
    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, resource: str | None) -> AsyncIterator[None]:
        if resource is None:
            yield
            return
        lock, users = self._locks.get(resource, (asyncio.Lock(), 0))
        self._locks[resource] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[resource]
            if users <= 1:
                del self._locks[resource]
            else:
                self._locks[resource] = (lock, users - 1)

    def __len__(self) -> int:
        return len(self._locks)
