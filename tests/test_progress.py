"""Live progress: the audit-to-progress mapping, the hub, and the streaming endpoint."""

import asyncio
import json

from orchestrator.observability.audit import AuditEvent, AuditLog
from orchestrator.observability.progress import ProgressHub, to_progress

from test_orchestrator import MVP, client_for, h, world  # noqa: F401  (world is a fixture)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for frame in text.split("\n\n"):
        lines = [line for line in frame.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        kind = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads("".join(line[6:] for line in lines if line.startswith("data: ")))
        events.append((kind, data))
    return events


# --- mapping ------------------------------------------------------------------------------------


def test_progress_carries_only_whitelisted_fields():
    event = AuditEvent(kind="task.finished", request_id="r", task_id="load", tool_id="file_processing",
                       data={"status": "succeeded", "duration_ms": 4.2, "deduplicated": False, "error": None,
                             "args": {"path": "users/alice/secret.csv"}, "user_id": "alice", "output": [1, 2]})
    msg = to_progress(event)
    assert msg == {"type": "task_finished", "task_id": "load", "tool": "file_processing", "status": "succeeded",
                   "duration_ms": 4.2, "deduplicated": False, "error": None}
    assert "secret" not in json.dumps(msg) and "alice" not in json.dumps(msg)


def test_non_progress_events_are_dropped():
    for kind in ("permission.decision", "planner.file_hints", "file.uploaded", "auth.forbidden"):
        assert to_progress(AuditEvent(kind=kind, request_id="r", data={"x": 1})) is None


# --- hub ------------------------------------------------------------------------------------------


async def test_hub_delivers_only_to_subscribers_of_that_request():
    hub = ProgressHub()
    audit = AuditLog(hub)
    mine, other = hub.subscribe("r1"), hub.subscribe("r2")
    audit.record("task.started", request_id="r1", task_id="a", tool_id="t", operation="run")
    audit.record("request.received", request_id="r3")  # nobody listening
    msg = await asyncio.wait_for(mine.get(), 1)
    assert msg["type"] == "task_started" and msg["task_id"] == "a" and "at" in msg
    assert other.drain() == []
    mine.close()
    other.close()
    assert hub.subscribers("r1") == hub.subscribers("r2") == 0


async def test_a_slow_client_drops_old_lines_instead_of_blocking():
    hub = ProgressHub()
    audit = AuditLog(hub)
    sub = hub.subscribe("r")
    for i in range(600):
        audit.record("task.started", request_id="r", task_id=f"t{i}", tool_id="x", operation="run")
    await asyncio.sleep(0.05)
    lines = sub.drain()
    assert len(lines) == 500 and lines[-1]["task_id"] == "t599"
    sub.close()


# --- streaming endpoint ---------------------------------------------------------------------------


async def test_stream_shows_each_step_then_the_record(world):
    with client_for(world) as c:
        r = c.post("/requests/stream", json={"text": MVP}, headers=h("alice"))
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "accepted" and kinds[-1] == "record"
    request_id = events[0][1]["request_id"]
    progress = [d for k, d in events if k == "progress"]
    types = [p["type"] for p in progress]
    assert types[0] == "received" and "planned" in types and "executing" in types and types[-1] == "finished"
    planned = next(p for p in progress if p["type"] == "planned")
    finished = {p["task_id"]: p["status"] for p in progress if p["type"] == "task_finished"}
    assert set(finished) == set(planned["tasks"]) and set(finished.values()) == {"succeeded"}
    started = [p["task_id"] for p in progress if p["type"] == "task_started"]
    assert set(started) == set(finished)
    final = events[-1][1]
    assert final["status"] == 200 and final["record"]["request_id"] == request_id
    assert final["record"]["state"] == "completed"
    assert "alice" not in json.dumps(progress)  # no user ids, arguments or outputs in progress


async def test_stream_of_a_rejected_request_ends_with_its_status(world):
    with client_for(world) as c:
        events = parse_sse(c.post("/requests/stream", json={"text": "book me a flight"}, headers=h("alice")).text)
    kind, final = events[-1]
    assert kind == "record" and final["status"] == 422
    assert final["record"]["error"]["code"] == "no_matching_tool"


async def test_stream_needs_a_valid_key(world):
    with client_for(world) as c:
        assert c.post("/requests/stream", json={"text": MVP}).status_code == 401
        assert c.post("/requests/stream", json={"text": ""}, headers=h("alice")).status_code == 422


async def test_stream_and_plain_endpoint_share_idempotency(world):
    with client_for(world) as c:
        first = c.post("/requests", json={"text": MVP}, headers=h("alice", **{"Idempotency-Key": "k1"})).json()
        events = parse_sse(c.post("/requests/stream", json={"text": MVP},
                                  headers=h("alice", **{"Idempotency-Key": "k1"})).text)
    assert events[-1][1]["record"]["request_id"] == first["request_id"]  # the earlier request, not a rerun


async def test_subscriptions_are_released_after_the_stream(world):
    with client_for(world) as c:
        events = parse_sse(c.post("/requests/stream", json={"text": MVP}, headers=h("alice")).text)
        hub = c.app.state.progress
        assert hub.subscribers(events[0][1]["request_id"]) == 0
