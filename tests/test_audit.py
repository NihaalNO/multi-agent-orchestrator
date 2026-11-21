import json

from orchestrator.observability import AuditLog, InMemoryAuditSink, JsonlAuditSink, redact


def test_redacts_secret_keys_recursively():
    data = {
        "query": "hello",
        "api_key": "abc",
        "headers": {"Authorization": "Bearer x", "accept": "json"},
        "items": [{"password": "p", "ok": 1}],
    }
    out = redact(data)
    assert out["query"] == "hello"
    assert out["api_key"] == "[REDACTED]"
    assert out["headers"] == {"Authorization": "[REDACTED]", "accept": "json"}
    assert out["items"] == [{"password": "[REDACTED]", "ok": 1}]


def test_record_fans_out_and_redacts(tmp_path):
    mem = InMemoryAuditSink()
    path = tmp_path / "audit" / "log.jsonl"
    log = AuditLog(mem, JsonlAuditSink(path))
    log.record("task.started", request_id="r1", task_id="t1", token="secret", args={"n": 1})
    [event] = mem.events()
    assert event.data == {"token": "[REDACTED]", "args": {"n": 1}}
    line = json.loads(path.read_text(encoding="utf-8").strip())
    assert line["kind"] == "task.started" and "secret" not in json.dumps(line)


def test_failing_sink_does_not_break_recording():
    class Boom:
        def emit(self, event):
            raise RuntimeError("disk full")

    mem = InMemoryAuditSink()
    AuditLog(Boom(), mem).record("x")
    assert len(mem.events()) == 1
