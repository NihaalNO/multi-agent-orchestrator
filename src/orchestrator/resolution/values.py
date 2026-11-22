"""Value helpers for comparing tool outputs: flattening, normalisation, timestamps, URLs."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Keys whose values say *when the source's information is from*. Used for recency and staleness.
TIMESTAMP_KEYS = frozenset({"as_of", "published_at", "updated_at", "observed_at", "timestamp"})
# Keys that differ between runs without saying anything about the answer.
DEFAULT_IGNORE_KEYS = frozenset({"retrieved_at", "stdout", "attempt", "backend", "duration_ms"})

_NUMERIC = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def flatten(value: Any, ignore: Iterable[str] = DEFAULT_IGNORE_KEYS, prefix: str = "") -> dict[str, Any]:
    """{'a': {'b': [1, 2]}} -> {'a.b.0': 1, 'a.b.1': 2}. Empty containers are kept as leaves."""
    ignore = frozenset(ignore)
    out: dict[str, Any] = {}
    if isinstance(value, dict) and value:
        for k, v in value.items():
            if k in ignore:
                continue
            out.update(flatten(v, ignore, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            out.update(flatten(v, ignore, f"{prefix}.{i}" if prefix else str(i)))
    else:
        out[prefix or "<value>"] = value
    return out


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def normalize(v: Any) -> Any:
    """Canonical form for comparison: numeric strings -> float, ints -> float, strings stripped.
    Deliberately conservative: no case folding, no unit conversion."""
    if isinstance(v, str):
        s = v.strip()
        if _NUMERIC.match(s):
            return float(s)
        return s
    if is_number(v):
        return float(v)
    return v


def values_equal(a: Any, b: Any, *, rel_tol: float, abs_tol: float) -> bool:
    if is_number(a) and is_number(b):
        if math.isnan(a) or math.isnan(b):
            return False
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    return a == b


def parse_timestamp(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, date):
        dt = datetime(v.year, v.month, v.day)
    elif isinstance(v, str) and v.strip():
        s = v.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def timestamps_in(value: Any) -> list[tuple[str, datetime]]:
    """All (path, timestamp) leaves whose key is a TIMESTAMP_KEY."""
    found = []
    for path, leaf in flatten(value, ignore=()).items():
        if path.rsplit(".", 1)[-1] in TIMESTAMP_KEYS:
            ts = parse_timestamp(leaf)
            if ts is not None:
                found.append((path, ts))
    return found


def source_time(value: Any) -> datetime | None:
    """The output's own 'as of' time: the shallowest timestamp leaf (latest if tied)."""
    found = timestamps_in(value)
    if not found:
        return None
    depth = min(p.count(".") for p, _ in found)
    return max(ts for p, ts in found if p.count(".") == depth)


_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_eid|ref)$", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Canonical URL for duplicate detection: lower-case scheme/host, no default port, no
    fragment, no tracking parameters, no trailing slash, sorted query."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)))
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, host, path, query, ""))
