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
