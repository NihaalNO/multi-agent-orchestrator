import asyncio
import os

import pytest

from orchestrator.contracts import ExecutionContext, Principal
from orchestrator.errors import PermissionDeniedError, ToolExecutionError
from orchestrator.registry import ToolRegistry
from orchestrator.tools import (
    FileProcessingTool,
    PythonExecTool,
    SearchResult,
    StaticSearchBackend,
    WebSearchTool,
)

CTX = ExecutionContext(request_id="r", execution_id="e", task_id="t", principal=Principal(user_id="u"))


@pytest.fixture
def uploads(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "sales.csv").write_text("month,Revenue\njan,100\nfeb,200\nmar,\napr,300\n", encoding="utf-8")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "more.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    return root


@pytest.fixture
def file_tool(uploads):
    return FileProcessingTool(uploads, max_bytes=1024)


async def test_all_initial_tools_register_cleanly(uploads):
    registry = ToolRegistry()
    await registry.register(FileProcessingTool(uploads))
    await registry.register(PythonExecTool())
    await registry.register(WebSearchTool(StaticSearchBackend([])))
    caps = registry.capabilities()
    assert {"data.tabular.load", "data.compute", "web.search", "file.read.text", "file.list"} <= set(caps)
    assert set((await registry.check_health()).values()) == {"healthy"}


# --- File processing -------------------------------------------------------------------------


async def test_read_csv(file_tool):
    out = await file_tool.invoke("read_csv", {"path": "sales.csv"}, CTX)
    assert out["columns"] == ["month", "Revenue"]
    assert out["row_count"] == 4 and not out["truncated"]
    assert out["rows"][0] == {"month": "jan", "Revenue": "100"}


async def test_read_csv_truncates(file_tool):
    out = await file_tool.invoke("read_csv", {"path": "sales.csv", "max_rows": 2}, CTX)
    assert out["row_count"] == 2 and out["truncated"]


async def test_read_text_and_list(file_tool):
    assert (await file_tool.invoke("read_text", {"path": "notes.txt"}, CTX))["content"] == "hello"
    assert (await file_tool.invoke("list_files", {}, CTX))["files"] == ["notes.txt", "sales.csv", "sub/more.csv"]
    assert (await file_tool.invoke("list_files", {"pattern": "*.csv"}, CTX))["files"] == ["sales.csv", "sub/more.csv"]


@pytest.mark.parametrize("bad", ["../secret.txt", "sub/../../secret.txt"])
async def test_path_traversal_rejected(file_tool, bad):
    with pytest.raises(PermissionDeniedError):
        await file_tool.invoke("read_text", {"path": bad}, CTX)


async def test_absolute_path_rejected(file_tool, tmp_path):
    with pytest.raises(PermissionDeniedError):
        await file_tool.invoke("read_text", {"path": str(tmp_path / "secret.txt")}, CTX)


async def test_symlink_escape_rejected(file_tool, uploads, tmp_path):
    link = uploads / "link.txt"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(PermissionDeniedError, match="escapes"):
        await file_tool.invoke("read_text", {"path": "link.txt"}, CTX)


async def test_missing_and_oversized_files(file_tool, uploads):
    with pytest.raises(ToolExecutionError, match="not found"):
        await file_tool.invoke("read_text", {"path": "nope.txt"}, CTX)
    (uploads / "big.txt").write_text("x" * 2000, encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="exceeds"):
        await file_tool.invoke("read_text", {"path": "big.txt"}, CTX)


# --- Python execution --------------------------------------------------------------------------


@pytest.fixture
def py():
    return PythonExecTool(timeout_s=5)


async def test_python_returns_result_and_stdout(py):
    out = await py.invoke("run", {"code": "print('hi')\nresult = sum(inputs['xs'])", "inputs": {"xs": [1, 2, 3]}}, CTX)
    assert out == {"result": 6, "stdout": "hi\n"}


async def test_python_exception_is_reported(py):
    with pytest.raises(ToolExecutionError) as exc:
        await py.invoke("run", {"code": "raise ValueError('bad input')"}, CTX)
    assert "bad input" in exc.value.details["traceback"]
    assert not exc.value.recoverable


async def test_python_non_json_result(py):
    with pytest.raises(ToolExecutionError) as exc:
        await py.invoke("run", {"code": "result = object()"}, CTX)
    assert "JSON" in exc.value.details["traceback"]


async def test_python_timeout_kills_process():
    tool = PythonExecTool(timeout_s=0.5)
    with pytest.raises(ToolExecutionError, match="time limit") as exc:
        await asyncio.wait_for(tool.invoke("run", {"code": "while True: pass"}, CTX), 10)
    assert exc.value.details["reason"] == "timeout" and not exc.value.recoverable


async def test_python_child_does_not_inherit_secrets(py, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    out = await py.invoke("run", {"code": "import os\nresult = os.environ.get('ANTHROPIC_API_KEY')"}, CTX)
    assert out["result"] is None
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-should-not-leak"


# --- Web search ---------------------------------------------------------------------------------


async def test_web_search_static_backend():
    backend = StaticSearchBackend([
        SearchResult(title="Retail revenue benchmarks 2026", url="https://example.com/a", snippet="median revenue"),
        SearchResult(title="Gardening tips", url="https://example.com/b"),
    ])
    tool = WebSearchTool(backend)
    registry = ToolRegistry()
    await registry.register(tool)
    out = await tool.invoke("search", {"query": "industry revenue benchmarks"}, CTX)
    assert [r["url"] for r in out["results"]] == ["https://example.com/a"]
    assert out["backend"] == "static"
    registry.validate_output("web_search", "search", out)


# --- Python execution on event loops without subprocess support ------------------------------------
# uvicorn uses asyncio.SelectorEventLoop on Windows with --reload or several workers; it cannot spawn
# subprocesses there (NotImplementedError). Found in the first live run.


def test_python_runs_inside_a_selector_event_loop():
    import asyncio as aio

    async def go():
        return await PythonExecTool(timeout_s=10).invoke("run", {"code": "result = inputs['x'] * 2", "inputs": {"x": 21}}, CTX)

    with aio.Runner(loop_factory=aio.SelectorEventLoop) as runner:
        assert runner.run(go())["result"] == 42


@pytest.fixture
def no_async_subprocess(monkeypatch):
    async def unsupported(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsupported)


async def test_threaded_fallback_runs_code(no_async_subprocess):
    out = await PythonExecTool(timeout_s=10).invoke("run", {"code": "print('hi')\nresult = sum(inputs)", "inputs": [1, 2, 3]}, CTX)
    assert out == {"result": 6, "stdout": "hi\n"}


async def test_threaded_fallback_timeout(no_async_subprocess):
    with pytest.raises(ToolExecutionError, match="time limit") as exc:
        await PythonExecTool(timeout_s=0.5).invoke("run", {"code": "while True: pass"}, CTX)
    assert exc.value.details["reason"] == "timeout"


async def test_threaded_fallback_cancellation_kills_child(no_async_subprocess, tmp_path):
    from test_execution import _pid_alive

    pid_file = tmp_path / "pid.txt"
    code = "import os, time\nopen(inputs, 'w').write(str(os.getpid()))\nwhile True: time.sleep(0.05)"
    task = asyncio.create_task(PythonExecTool(timeout_s=30).invoke("run", {"code": code, "inputs": str(pid_file)}, CTX))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    assert _pid_alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _pid_alive(pid)



# --- Over-escaped code from LLM planners (found in the first live Groq run) -------------------------

from orchestrator.tools.python_exec import SPEC as PY_SPEC, repair_escaped_code  # noqa: E402


def test_over_escaped_code_is_repaired():
    broken = "rows = inputs['rows']\\nvalues = [float(r['v']) for r in rows]\\nresult = sum(values)"
    fixed = repair_escaped_code(broken)
    assert fixed == "rows = inputs['rows']\nvalues = [float(r['v']) for r in rows]\nresult = sum(values)"


@pytest.mark.parametrize("code", [
    "result = 'a\\nb'",                      # valid one-liner with \\n inside a string literal
    "x = 1\nresult = 'line\\n'",            # real newlines present: left alone
    "result = (",                             # genuinely broken, nothing to repair
    "def f(:\\n    pass",                     # repair would still not compile: left alone
])
def test_valid_or_unrepairable_code_is_untouched(code):
    assert repair_escaped_code(code) == code


async def test_over_escaped_code_runs():
    code = "values = [float(r['Revenue']) for r in inputs['rows']]\\nresult = sum(values) / len(values)"
    out = await PythonExecTool(timeout_s=10).invoke(
        "run", {"code": code, "inputs": {"rows": [{"Revenue": "100"}, {"Revenue": "300"}]}}, CTX)
    assert out["result"] == 200.0


def test_python_inputs_must_be_an_object():
    from jsonschema import Draft202012Validator

    v = Draft202012Validator(PY_SPEC.operation("run").input_schema)
    assert v.is_valid({"code": "result = 1", "inputs": {"rows": []}})
    assert not v.is_valid({"code": "result = 1", "inputs": [{"a": 1}]})



@pytest.mark.parametrize("broken,expected_result", [
    # seen live from Groq: every line after the first shifted by one space
    ("rows = inputs['rows']\n total = 0.0\n for r in rows:\n     total += float(r['v'])\n result = total", 3.0),
    # every line shifted by the same amount
    ("    x = 1\n    result = x + 2", 3),
])
async def test_uniformly_misindented_code_is_repaired(broken, expected_result):
    out = await PythonExecTool(timeout_s=10).invoke("run", {"code": broken, "inputs": {"rows": [{"v": "1"}, {"v": "2"}]}}, CTX)
    assert out["result"] == expected_result


def test_genuinely_wrong_indentation_is_left_alone():
    code = "if True:\nresult = 1"  # missing indentation: no mechanical fix, keep the real error
    assert repair_escaped_code(code) == code
