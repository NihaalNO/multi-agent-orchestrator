"""Python Execution tool.

Runs a code snippet in a **separate interpreter process**:

* ``python -I`` (isolated mode: ignores ``PYTHON*`` env vars and user site-packages)
* fresh temporary working directory, deleted afterwards
* minimal environment (no inherited secrets such as API keys)
* wall-clock timeout, after which the process is killed
* capped stdout/stderr

The snippet receives a JSON value as ``inputs`` and must assign a JSON-serialisable value to
``result``. Anything printed is captured as ``stdout``.

LIMITATION: a subprocess is not a security sandbox. Code can still read files the OS user can
read and open network connections. Container isolation (``IsolationLevel.CONTAINER``) is the
planned upgrade. Permission policy should therefore treat this tool's ``execute`` access as
privileged.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from typing import Any

from orchestrator.contracts import (
    AccessLevel,
    ExecutionConstraints,
    ExecutionContext,
    IsolationLevel,
    OperationSpec,
    ToolSpec,
)
from orchestrator.errors import ToolExecutionError
from orchestrator.tools.base import ToolAdapter

MAX_CODE_CHARS = 20_000
MAX_OUTPUT_BYTES = 256 * 1024
_RESULT_MARKER = "\x1e__ORCH_RESULT__"

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
    status = {"ok": False, "error": traceback.format_exc(limit=5)}
out = {"stdout": buf.getvalue(), **status}
if status["ok"]:
    try:
        json.dumps(ns["result"])
        out["result"] = ns["result"]
    except (TypeError, ValueError) as exc:
        out = {"stdout": buf.getvalue(), "ok": False, "error": f"result is not JSON-serialisable: {exc}"}
sys.__stdout__.write(MARKER + json.dumps(out))
""".replace("MARKER", repr(_RESULT_MARKER))

SPEC = ToolSpec(
    tool_id="python_exec",
    name="Python Execution",
    description=(
        "Runs a short Python snippet in a separate process. The snippet reads the variable "
        "`inputs` and must assign its answer to `result` (JSON-serialisable). It runs in an empty "
        "temporary folder and cannot open uploaded files: load data with a file tool first and pass "
        "it in through `inputs`."
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


class PythonExecTool(ToolAdapter):
    spec = SPEC

    def __init__(self, *, timeout_s: float = 10.0, python: str | None = None) -> None:
        self._timeout = timeout_s
        self._python = python or sys.executable

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
        with tempfile.TemporaryDirectory(prefix="orch_py_", ignore_cleanup_errors=True) as workdir:
            try:
                returncode, stdout, stderr = await self._run_async(payload, workdir)
            except NotImplementedError:
                returncode, stdout, stderr = await self._run_threaded(payload, workdir)

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
