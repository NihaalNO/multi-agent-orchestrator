"""Python Execution tool.

Two backends, chosen by ``ORCH_PYTHON_SANDBOX`` (see ``build_python_tool``):

* **WebAssembly** (``IsolationLevel.WASM``, preferred): CPython compiled for WASI runs in wasmtime
  with no host files or network, a memory cap and a runtime-enforced time limit. Standard library
  only. See ``orchestrator.tools.wasm_python``.
* **Subprocess** (``IsolationLevel.SUBPROCESS``, fallback): ``python -I`` in a fresh temporary
  folder with a minimal environment (no inherited secrets), a wall-clock timeout and capped output.
  NOT a security sandbox: code can read files the OS user can read and use the network.

Either way the snippet receives a JSON value as ``inputs`` and must assign a JSON-serialisable value
to ``result``. Anything printed is captured as ``stdout``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from orchestrator.contracts import (
    AccessLevel,
    ExecutionConstraints,
    ExecutionContext,
    IsolationLevel,
    OperationSpec,
    ToolSpec,
)
from orchestrator.errors import ConfigurationError, ToolExecutionError
from orchestrator.tools.base import ToolAdapter
from orchestrator.tools.wasm_python import WasmPython, wasm_unavailable_reason

MAX_CODE_CHARS = 20_000
MAX_OUTPUT_BYTES = 256 * 1024
_RESULT_MARKER = "\x1e__ORCH_RESULT__"
# Printed output kept per run (its end). Well under MAX_OUTPUT_BYTES, which must also hold
# the JSON-encoded result; a result too large for the cap is still reported as an error.
_KEEP_STDOUT_CHARS = 64_000

# Executed by the child interpreter. Reads {"code", "inputs"} from stdin, runs the code with
# stdout redirected, then writes one marker-prefixed JSON line to the real stdout.
_RUNNER = r"""
import contextlib, io, json, sys, traceback
payload = json.loads(sys.stdin.read())
buf = io.StringIO()
ns = {"inputs": payload["inputs"], "result": None, "__name__": "__orch__"}
status = {"ok": True}
try:
    with contextlib.redirect_stdout(buf):
        exec(compile(payload["code"], "<task>", "exec"), ns)
except BaseException:
    status = {"ok": False, "error": traceback.format_exc(limit=5)[-4000:]}
# Keep only the end of long printed output so the result line always fits under the output cap.
printed = buf.getvalue()
if len(printed) > KEEP:
    printed = f"[... {len(printed) - KEEP} earlier characters of output dropped]\n" + printed[-KEEP:]
out = {"stdout": printed, **status}
if status["ok"]:
    try:
        json.dumps(ns["result"])
        out["result"] = ns["result"]
    except (TypeError, ValueError) as exc:
        out = {"stdout": printed, "ok": False, "error": f"result is not JSON-serialisable: {exc}"}
sys.__stdout__.write(MARKER + json.dumps(out))
""".replace("MARKER", repr(_RESULT_MARKER)).replace("KEEP", str(_KEEP_STDOUT_CHARS))

SPEC = ToolSpec(
    tool_id="python_exec",
    name="Python Execution",
    description=(
        "Runs a short Python snippet in an isolated sandbox. The snippet reads the variable "
        "`inputs` and must assign its answer to `result` (JSON-serialisable). Only the Python "
        "standard library is available (no numpy or pandas). It cannot open uploaded files or use "
        "the network: load data with a file tool first and pass it in through `inputs`."
    ),
    reliability=0.95,
    constraints=ExecutionConstraints(
        # Engine-level timeout sits above the adapter's own kill timer so the adapter can
        # report a clean "code exceeded time limit" error and reap the process first.
        timeout_s=15, max_retries=0, max_concurrency=2, expected_latency_ms=300,
        isolation=IsolationLevel.SUBPROCESS,
    ),
    operations=(
        OperationSpec(
            name="run",
            description="Execute Python code against `inputs` and return `result`.",
            capabilities=("code.python.execute", "data.compute"),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string", "minLength": 1, "maxLength": MAX_CODE_CHARS,
                        "description": "Python source. It reads the variable `inputs` and must assign a "
                                       "JSON-serialisable answer to the variable `result`.",
                    },
                    "inputs": {
                        "type": "object",
                        "description": "A JSON object; the code receives it unchanged as the variable `inputs`. "
                                       "Name each value, e.g. {\"rows\": {\"$ref\": \"load_file.rows\"}}, and "
                                       "read it as inputs['rows']. Never pass a bare $ref or list here.",
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"result": {}, "stdout": {"type": "string"}},
                "required": ["result", "stdout"],
            },
            access=AccessLevel.EXECUTE,
            # Re-running the same code has no external side effects beyond the temp dir, but
            # we cannot prove that for arbitrary code, so do not claim idempotency.
            idempotent=False,
        ),
    ),
)


def _compiles(code: str) -> bool:
    try:
        compile(code, "<task>", "exec")  # parses only; nothing is executed
    except SyntaxError:
        return False
    return True


def repair_escaped_code(code: str) -> str:
    """Undo formatting slips LLM planners make in code (see ``_repair_candidates``).

    Applied only when the code as given does not compile, and only if a repaired version does
    compile — so valid code (including code with ``"\\n"`` inside string literals) is never changed,
    and nothing is executed to decide.
    """
    if _compiles(code):
        return code
    for candidate in _repair_candidates(code):
        if _compiles(candidate):
            return candidate
    return code


def _repair_candidates(code: str) -> list[str]:
    """Formatting slips seen from LLM planners, each undone mechanically:
    1. literal ``\\n`` instead of line breaks (one line of over-escaped code);
    2. every line indented by the same extra amount;
    3. every line after the first indented by the same extra amount (e.g. "a\\n b\\n c")."""
    import textwrap

    out = []
    if "\n" not in code and "\\n" in code:
        out.append(code.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t"))
    out.append(textwrap.dedent(code))
    first, _, rest = code.partition("\n")
    if rest:
        out.append(first + "\n" + textwrap.dedent(rest))
    return out


def _child_env() -> dict[str, str]:
    """Only what the interpreter needs to start. Notably excludes API keys and tokens."""
    keep = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if os.name == "nt" else ("PATH", "TMPDIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


_WASM_SPEC = SPEC.model_copy(update={
    "constraints": SPEC.constraints.model_copy(update={"isolation": IsolationLevel.WASM}),
})


class PythonExecTool(ToolAdapter):
    spec = SPEC

    def __init__(self, *, timeout_s: float = 10.0, python: str | None = None,
                 sandbox: WasmPython | None = None) -> None:
        self._timeout = timeout_s
        self._python = python or sys.executable
        self._sandbox = sandbox
        if sandbox is not None:
            self.spec = _WASM_SPEC

    @property
    def isolation(self) -> IsolationLevel:
        return self.spec.constraints.isolation

    async def health_check(self) -> bool:
        return self._sandbox is None or await asyncio.to_thread(self._sandbox.ready, 60)

    async def _run_wasm(self, payload: bytes) -> tuple[int | None, bytes, bytes]:
        assert self._sandbox is not None
        # Runs in a worker thread. If this task is cancelled the thread finishes on its own, but the
        # runtime's time limit bounds it, so nothing keeps running past ``timeout_s``.
        res = await asyncio.to_thread(
            self._sandbox.run, ["python", "-I", "-S", "-c", _RUNNER], payload, self._timeout)
        if res.timed_out:
            raise self._timed_out()
        code = res.exit_code if res.exit_code is not None else -1
        return code, res.stdout, res.stderr

    def _timed_out(self) -> ToolExecutionError:
        # Re-running the same code would time out again, so this is not recoverable.
        return ToolExecutionError(
            f"code exceeded the {self._timeout}s time limit",
            details={"reason": "timeout", "timeout_s": self._timeout},
        )

    async def _run_async(self, payload: bytes, workdir: str) -> tuple[int | None, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            self._python, "-I", "-c", _RUNNER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=_child_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(payload), self._timeout)
        except asyncio.CancelledError:
            # Engine timeout or execution cancelled: never leave the child running.
            if proc.returncode is None:
                proc.kill()
                await asyncio.shield(proc.wait())
            raise
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise self._timed_out() from None
        return proc.returncode, stdout, stderr

    async def _run_threaded(self, payload: bytes, workdir: str) -> tuple[int | None, bytes, bytes]:
        """Same contract as ``_run_async`` for event loops that cannot spawn subprocesses — notably
        the ``SelectorEventLoop`` uvicorn uses on Windows with ``--reload`` or several workers."""
        proc = subprocess.Popen(
            [self._python, "-I", "-c", _RUNNER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=workdir, env=_child_env(),
        )
        try:
            stdout, stderr = await asyncio.to_thread(proc.communicate, payload, self._timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.communicate)
            raise self._timed_out() from None
        except asyncio.CancelledError:
            # The thread keeps waiting in communicate(); killing the child ends it promptly.
            proc.kill()
            proc.wait(timeout=5)
            raise
        return proc.returncode, stdout, stderr

    async def op_run(self, args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        payload = json.dumps({"code": repair_escaped_code(args["code"]), "inputs": args.get("inputs")}).encode()
        if self._sandbox is not None:
            return self._parse(*await self._run_wasm(payload))
        with tempfile.TemporaryDirectory(prefix="orch_py_", ignore_cleanup_errors=True) as workdir:
            try:
                returncode, stdout, stderr = await self._run_async(payload, workdir)
            except NotImplementedError:
                returncode, stdout, stderr = await self._run_threaded(payload, workdir)
        return self._parse(returncode, stdout, stderr)

    def _parse(self, returncode: int | None, stdout: bytes, stderr: bytes) -> dict[str, Any]:
        text = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        marker_at = text.rfind(_RESULT_MARKER)
        if returncode != 0 or marker_at < 0:
            err = stderr[-4000:].decode("utf-8", errors="replace")
            raise ToolExecutionError(
                "python process failed",
                details={"exit_code": returncode, "stderr": err},
            )
        try:
            out = json.loads(text[marker_at + len(_RESULT_MARKER):])
        except json.JSONDecodeError as exc:
            raise ToolExecutionError("could not decode python result (output too large?)") from exc
        if not out.get("ok"):
            raise ToolExecutionError(
                "python code raised an error",
                details={"traceback": out.get("error", "")[-4000:], "stdout": out.get("stdout", "")[-2000:]},
            )
        return {"result": out.get("result"), "stdout": out.get("stdout", "")[-MAX_OUTPUT_BYTES:]}


_SHARED: dict[tuple[str, int], WasmPython] = {}
_SHARED_LOCK = threading.Lock()


def shared_wasm_python(wasm_dir: Path, memory_mb: int) -> WasmPython:
    """One compiled sandbox per (folder, memory cap) per process: compiling python.wasm takes about
    a second and a half, and runs are independent instances anyway."""
    key = (str(Path(wasm_dir).resolve()), memory_mb)
    with _SHARED_LOCK:
        if key not in _SHARED:
            _SHARED[key] = WasmPython(wasm_dir, memory_mb=memory_mb, max_output_bytes=MAX_OUTPUT_BYTES)
        return _SHARED[key]


def build_python_tool(*, sandbox: str, wasm_dir: Path, timeout_s: float, memory_mb: int) -> PythonExecTool:
    """``sandbox``: "wasm" (required: raises ConfigurationError if unavailable), "subprocess", or
    "auto" (WebAssembly when available, otherwise the subprocess fallback)."""
    if sandbox == "subprocess":
        return PythonExecTool(timeout_s=timeout_s)
    reason = wasm_unavailable_reason(wasm_dir)
    if reason is None:
        return PythonExecTool(timeout_s=timeout_s, sandbox=shared_wasm_python(wasm_dir, memory_mb))
    if sandbox == "wasm":
        raise ConfigurationError(f"ORCH_PYTHON_SANDBOX=wasm but the sandbox is unavailable: {reason}")
    return PythonExecTool(timeout_s=timeout_s)
