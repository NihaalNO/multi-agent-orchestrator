"""Durable state in SQLite: request records, approvals and idempotency results.

Enabled by ``ORCH_STATE_PATH``; without it everything stays in memory, as before.

* One SQLite file, opened once per process, WAL journal (readers never block the writer),
  ``synchronous=NORMAL``. Records are stored as the same JSON their Pydantic models produce, plus
  a few indexed columns for the queries the stores need.
* Calls are synchronous: each is a single indexed row read or write, far below a millisecond on
  local disk, so running them on the event loop is simpler than a thread hop and costs nothing
  measurable. A ``threading.Lock`` guards the shared connection.
* **Schema versioning** via ``PRAGMA user_version``. A database written by a newer version of the
  orchestrator is refused instead of being misread.
* **Restart recovery**: requests still ``running`` when the process stopped are marked ``failed``
  (``interrupted_by_restart``) on open. They are never re-run automatically — a half-finished
  request may already have had side effects.
* The file holds request text, tool outputs and approval previews: treat it like the audit log
  (restrict access; on POSIX it is created ``0600``).

Single instance only: the in-process locks around requests and writes do not extend to another
process using the same file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.contracts import ApprovalRequest, ErrorInfo, TaskResult
from orchestrator.core import RequestRecord, RequestState, RequestStore
from orchestrator.errors import ConfigurationError
from orchestrator.permissions.approvals import _LIVE

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    state        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_state ON requests(state);
CREATE TABLE IF NOT EXISTS request_keys (
    user_id         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id  TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    expires_at   REAL NOT NULL,
    live_key     TEXT,            -- set while pending/approved: dedup lookup for identical calls
    created_at   TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_live ON approvals(live_key);
CREATE INDEX IF NOT EXISTS approvals_expiry ON approvals(status, expires_at);
CREATE TABLE IF NOT EXISTS idempotency (
    key          TEXT PRIMARY KEY,
    expires_at   REAL NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idempotency_expiry ON idempotency(expires_at);
"""


class StateDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        except sqlite3.Error as exc:
            raise ConfigurationError(f"cannot open state database {self.path}: {exc}") from exc
        if new and os.name == "posix":
            os.chmod(self.path, 0o600)
        self.lock = threading.Lock()
        with self.lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                self._conn.close()
                raise ConfigurationError(
                    f"{self.path} was written by a newer orchestrator (schema {version} > {SCHEMA_VERSION})"
                )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self.lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self.lock:
            self._conn.close()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class SqliteRequestStore(RequestStore):
    """Same interface and in-process lock as the in-memory store; rows live in SQLite."""

    def __init__(self, db: StateDB) -> None:
        super().__init__()
        self._db = db

    def get(self, request_id: str) -> RequestRecord | None:
        rows = self._db.execute("SELECT data FROM requests WHERE request_id = ?", (request_id,))
        return RequestRecord.model_validate_json(rows[0][0]) if rows else None

    def put(self, record: RequestRecord) -> None:
        self._db.execute(
            "INSERT INTO requests(request_id, user_id, state, updated_at, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(request_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at, "
            "data=excluded.data",
            (record.request_id, record.user_id, record.state.value, _iso(record.updated_at), record.model_dump_json()),
        )

    def by_key(self, user_id: str, key: str) -> RequestRecord | None:
        rows = self._db.execute(
            "SELECT request_id FROM request_keys WHERE user_id = ? AND idempotency_key = ?", (user_id, key)
        )
        return self.get(rows[0][0]) if rows else None

    def bind_key(self, user_id: str, key: str, request_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO request_keys(user_id, idempotency_key, request_id) VALUES (?, ?, ?)",
            (user_id, key, request_id),
        )

    def recover_interrupted(self) -> list[str]:
        """Mark requests left ``running`` by a previous process as failed. Returns their ids."""
        recovered = []
        for (data,) in self._db.execute("SELECT data FROM requests WHERE state = ?", (RequestState.RUNNING.value,)):
            record = RequestRecord.model_validate_json(data)
            failed = record.model_copy(update={
                "state": RequestState.FAILED,
                "updated_at": datetime.now(UTC),
                "error": ErrorInfo(
                    code="interrupted_by_restart",
                    message="the orchestrator restarted while this request was running; it was not re-run "
                            "automatically because it may already have had side effects",
                ),
            })
            self.put(failed)
            recovered.append(record.request_id)
        return recovered


def _live_key(item: ApprovalRequest) -> str | None:
    if item.status not in _LIVE:
        return None
    return json.dumps([item.requested_by, item.request_id, item.task_id, item.tool_id, item.operation,
                       item.args_digest])


class SqliteApprovalStore:
    def __init__(self, db: StateDB) -> None:
        self._db = db

    def get(self, approval_id: str) -> ApprovalRequest | None:
        rows = self._db.execute("SELECT data FROM approvals WHERE approval_id = ?", (approval_id,))
        return ApprovalRequest.model_validate_json(rows[0][0]) if rows else None

    def put(self, item: ApprovalRequest) -> None:
        self._db.execute(
            "INSERT INTO approvals(approval_id, status, expires_at, live_key, created_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status, "
            "expires_at=excluded.expires_at, live_key=excluded.live_key, data=excluded.data",
            (item.approval_id, item.status.value, item.expires_at.timestamp(), _live_key(item),
             _iso(item.created_at), item.model_dump_json()),
        )

    def all(self) -> list[ApprovalRequest]:
        return [ApprovalRequest.model_validate_json(d)
                for (d,) in self._db.execute("SELECT data FROM approvals ORDER BY created_at")]

    def find_live(self, requested_by: str, request_id: str, task_id: str, tool_id: str,
                  operation: str, digest: str) -> ApprovalRequest | None:
        key = json.dumps([requested_by, request_id, task_id, tool_id, operation, digest])
        rows = self._db.execute("SELECT data FROM approvals WHERE live_key = ? LIMIT 1", (key,))
        return ApprovalRequest.model_validate_json(rows[0][0]) if rows else None

    def due_for_expiry(self, now: datetime) -> list[ApprovalRequest]:
        statuses = tuple(s.value for s in _LIVE)
        rows = self._db.execute(
            f"SELECT data FROM approvals WHERE status IN ({','.join('?' * len(statuses))}) AND expires_at <= ?",
            (*statuses, now.timestamp()),
        )
        return [ApprovalRequest.model_validate_json(d) for (d,) in rows]


class SqliteIdempotencyBackend:
    def __init__(self, db: StateDB) -> None:
        self._db = db

    def get(self, key: str, now: float) -> TaskResult | None:
        rows = self._db.execute("SELECT data FROM idempotency WHERE key = ? AND expires_at > ?", (key, now))
        return TaskResult.model_validate_json(rows[0][0]) if rows else None

    def put(self, key: str, expires_at: float, result: TaskResult) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO idempotency(key, expires_at, data) VALUES (?, ?, ?)",
            (key, expires_at, result.model_dump_json()),
        )

    def evict(self, now: float) -> None:
        self._db.execute("DELETE FROM idempotency WHERE expires_at <= ?", (now,))


@dataclass
class PersistentState:
    db: StateDB
    requests: SqliteRequestStore
    approvals: SqliteApprovalStore
    idempotency: SqliteIdempotencyBackend
    recovered: list[str]

    def close(self) -> None:
        self.db.close()


def open_state(path: Path) -> PersistentState:
    db = StateDB(path)
    requests = SqliteRequestStore(db)
    return PersistentState(
        db=db,
        requests=requests,
        approvals=SqliteApprovalStore(db),
        idempotency=SqliteIdempotencyBackend(db),
        recovered=requests.recover_interrupted(),
    )
