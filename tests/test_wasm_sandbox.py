"""The WebAssembly Python sandbox: results, and the isolation guarantees it is there for.

Needs wasmtime and python.wasm (``python scripts/fetch_python_wasm.py``); CI fetches them. Tests
that need the sandbox are skipped without it, the configuration tests always run.
"""

import time
from pathlib import Path

import pytest

from orchestrator.__main__ import check_config
from orchestrator.config import Settings
from orchestrator.contracts import ExecutionContext, IsolationLevel, Principal
from orchestrator.errors import ConfigurationError, ToolExecutionError
from orchestrator.tools.python_exec import build_python_tool
from orchestrator.tools.wasm_python import wasm_unavailable_reason

WASM_DIR = Path(__file__).parents[1] / "data" / "wasi" / "python-3.15.2"
REASON = wasm_unavailable_reason(WASM_DIR)
needs_wasm = pytest.mark.skipif(REASON is not None, reason=f"WebAssembly sandbox unavailable: {REASON}")
CTX = ExecutionContext(request_id="r", execution_id="e", task_id="t", principal=Principal(user_id="u"))


def tool(timeout_s=5.0, memory_mb=64):
    return build_python_tool(sandbox="wasm", wasm_dir=WASM_DIR, timeout_s=timeout_s, memory_mb=memory_mb)


async def run(code, inputs=None, **kw):
    return await tool(**kw).invoke("run", {"code": code, "inputs": inputs or {}}, CTX)


async def fails(code, **kw) -> str:
    with pytest.raises(ToolExecutionError) as info:
        await run(code, **kw)
    return f"{info.value.message} {info.value.details}"


# --- results ---------------------------------------------------------------------------------


@needs_wasm
async def test_runs_code_against_inputs_with_the_standard_library():
    out = await run("import statistics\nresult = statistics.median(float(r['v']) for r in inputs['rows'])\nprint('ok')",
                    {"rows": [{"v": "1"}, {"v": "5"}, {"v": "3"}]})
    assert out == {"result": 3.0, "stdout": "ok\n"}


@needs_wasm
async def test_errors_come_back_as_tracebacks():
    assert "ZeroDivisionError" in await fails("result = 1 / 0")


@needs_wasm
def test_spec_reports_the_wasm_isolation_level():
    assert tool().spec.constraints.isolation == IsolationLevel.WASM


# --- isolation -------------------------------------------------------------------------------


@needs_wasm
async def test_host_files_are_invisible(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    detail = await fails(f"result = open({secret.as_posix()!r}).read()")
    assert "top secret" not in detail
    assert "FileNotFoundError" in detail or "No such file" in detail


@needs_wasm
async def test_only_lib_and_tmp_are_visible():
    out = await run("import os\nresult = sorted(os.listdir('/lib')) + sorted(os.listdir('/tmp'))")
    assert all(name.startswith("python3") for name in out["result"])  # stdlib only; /tmp starts empty


@needs_wasm
async def test_standard_library_is_read_only():
    assert "PermissionError" in await fails("open('/lib/evil.py', 'w').write('x')")


@needs_wasm
async def test_scratch_folder_is_writable_and_fresh_each_run():
    out = await run("open('/tmp/a.txt', 'w').write('hi')\nresult = open('/tmp/a.txt').read()")
    assert out["result"] == "hi"
    again = await run("import os\nresult = os.listdir('/tmp')")
    assert again["result"] == []


@needs_wasm
async def test_no_network():
    detail = await fails("import socket\nsocket.create_connection(('example.com', 80), timeout=2)")
    assert "Error" in detail


@needs_wasm
async def test_no_host_environment_or_secrets(monkeypatch):
    monkeypatch.setenv("ORCH_TEST_SECRET", "should-not-leak")
    out = await run("import os\nresult = sorted(os.environ)")
    assert "ORCH_TEST_SECRET" not in out["result"]
    assert set(out["result"]) <= {"PYTHONHOME", "TMPDIR", "HOME"}


# --- limits ----------------------------------------------------------------------------------


@needs_wasm
async def test_infinite_loop_is_stopped_at_the_time_limit():
    started = time.perf_counter()
    detail = await fails("while True:\n    pass", timeout_s=1.0)
    assert "time limit" in detail
    assert time.perf_counter() - started < 3


@needs_wasm
async def test_memory_cap():
    assert "MemoryError" in await fails("x = bytearray(200 * 1024 * 1024)", memory_mb=64)


@pytest.mark.parametrize("sandbox", [pytest.param("wasm", marks=needs_wasm), "subprocess"])
async def test_lots_of_printing_keeps_the_result_and_the_end_of_the_output(sandbox):
    t = build_python_tool(sandbox=sandbox, wasm_dir=WASM_DIR, timeout_s=10, memory_mb=128)
    code = "import sys\nfor i in range(2000):\n    sys.stdout.write('x' * 999 + '\\n')\nprint('last line')\nresult = 1"
    out = await t.invoke("run", {"code": code, "inputs": {}}, CTX)
    assert out["result"] == 1
    assert out["stdout"].startswith("[... ") and out["stdout"].endswith("last line\n")
    assert len(out["stdout"]) < 70_000


@needs_wasm
async def test_runs_do_not_share_state():
    await run("import builtins\nbuiltins.LEAK = 42\nresult = 1")
    out = await run("import builtins\nresult = hasattr(builtins, 'LEAK')")
    assert out["result"] is False


# --- configuration ---------------------------------------------------------------------------


def test_wasm_mode_refuses_to_start_without_the_sandbox(tmp_path):
    with pytest.raises(ConfigurationError, match="python.wasm"):
        build_python_tool(sandbox="wasm", wasm_dir=tmp_path, timeout_s=5, memory_mb=64)
    errors, _ = check_config(Settings(python_sandbox="wasm", python_wasm_dir=tmp_path), environ={})
    assert any("ORCH_PYTHON_SANDBOX=wasm" in e for e in errors)


def test_auto_mode_falls_back_to_a_subprocess_with_a_warning(tmp_path):
    t = build_python_tool(sandbox="auto", wasm_dir=tmp_path, timeout_s=5, memory_mb=64)
    assert t.isolation == IsolationLevel.SUBPROCESS
    _, warnings = check_config(Settings(python_sandbox="auto", python_wasm_dir=tmp_path), environ={})
    assert any("not a security sandbox" in w for w in warnings)


def test_subprocess_mode_is_flagged():
    t = build_python_tool(sandbox="subprocess", wasm_dir=WASM_DIR, timeout_s=5, memory_mb=64)
    assert t.isolation == IsolationLevel.SUBPROCESS
    _, warnings = check_config(Settings(python_sandbox="subprocess"), environ={})
    assert any("not sandboxed" in w for w in warnings)


@needs_wasm
def test_one_compiled_sandbox_is_shared_per_process():
    assert tool()._sandbox is tool()._sandbox
