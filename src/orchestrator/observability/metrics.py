"""Latency and reliability metrics, derived from audit events.

``MetricsSink`` is an ``AuditSink``: plug it into the ``AuditLog`` and it aggregates the events
the system already emits (``task.finished``, ``task.retry``, ``execution.finished``,
``conflict.*``, ``request.finished``). Because the numbers come from the audit stream they
always agree with the audit log. In-memory and per process; export to OpenTelemetry later.
"""

from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict, deque
from typing import Any

from orchestrator.observability.audit import AuditEvent

_WINDOW = 1000  # latency samples kept per series


def _percentile(samples: list[float], q: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    k = max(0, math.ceil(q * len(ordered)) - 1)
    return round(ordered[k], 3)


class _Series:
    def __init__(self) -> None:
        self.statuses: Counter[str] = Counter()
        self.latencies: deque[float] = deque(maxlen=_WINDOW)
        self.retries = 0

    def snapshot(self) -> dict[str, Any]:
        total = sum(self.statuses.values())
        lat = list(self.latencies)
        return {
            "count": total,
            "statuses": dict(self.statuses),
            "success_rate": round(self.statuses.get("succeeded", 0) / total, 4) if total else None,
            "retries": self.retries,
            "latency_ms": {"p50": _percentile(lat, 0.5), "p95": _percentile(lat, 0.95), "max": max(lat, default=None)},
        }


class MetricsSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tools: dict[str, _Series] = defaultdict(_Series)
        self._executions: Counter[str] = Counter()
        self._conflicts: Counter[str] = Counter()
        self._requests: Counter[str] = Counter()
        self._request_latency: deque[float] = deque(maxlen=_WINDOW)

    def emit(self, event: AuditEvent) -> None:
        d = event.data
        with self._lock:
            if event.kind == "task.finished" and event.tool_id:
                series = self._tools[event.tool_id]
                series.statuses[d.get("status", "unknown")] += 1
                if d.get("duration_ms") is not None and not d.get("deduplicated"):
                    series.latencies.append(float(d["duration_ms"]))
            elif event.kind == "task.retry" and event.tool_id:
                self._tools[event.tool_id].retries += 1
            elif event.kind == "execution.finished":
                self._executions[d.get("status", "unknown")] += 1
            elif event.kind.startswith("conflict."):
                self._conflicts[f"{d.get('conflict_kind', 'unknown')}:{event.kind.split('.', 1)[1]}"] += 1
            elif event.kind == "request.finished":
                self._requests[d.get("state", "unknown")] += 1
                if d.get("duration_ms") is not None:
                    self._request_latency.append(float(d["duration_ms"]))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lat = list(self._request_latency)
            return {
                "requests": {
                    "states": dict(self._requests),
                    "latency_ms": {"p50": _percentile(lat, 0.5), "p95": _percentile(lat, 0.95)},
                },
                "executions": dict(self._executions),
                "tools": {tid: s.snapshot() for tid, s in sorted(self._tools.items())},
                "conflicts": dict(self._conflicts),
            }
