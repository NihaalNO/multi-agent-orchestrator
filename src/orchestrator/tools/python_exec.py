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
        "`inputs` and must assign its answer to `result` (JSON-serialisable)."
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
                    "code": {"type": "string", "minLength": 1, "maxLength": MAX_CODE_CHARS},
                    "inputs": {},
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

    async def op_run(self, args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        payload = json.dumps({"code": args["code"], "inputs": args.get("inputs")}).encode()
        with tempfile.TemporaryDirectory(prefix="orch_py_") as workdir:
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
            except TimeoutError:
                proc.kill()
                await proc.wait()
                # Re-running the same code would time out again, so this is not recoverable.
                raise ToolExecutionError(
                    f"code exceeded the {self._timeout}s time limit",
                    details={"reason": "timeout", "timeout_s": self._timeout},
                ) from None

        text = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        marker_at = text.rfind(_RESULT_MARKER)
        if proc.returncode != 0 or marker_at < 0:
            err = stderr[-4000:].decode("utf-8", errors="replace")
            raise ToolExecutionError(
                "python process failed",
                details={"exit_code": proc.returncode, "stderr": err},
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
