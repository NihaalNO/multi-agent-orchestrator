"""Python inside WebAssembly: the sandbox for the Python Execution tool.

CPython compiled for WASI (``python.wasm``, see ``scripts/fetch_python_wasm.py``) runs in the
wasmtime runtime. Each run gets a fresh instance with:

* **no host files** except the standard library, mounted **read-only** at ``/lib``, and an empty
  scratch folder at ``/tmp`` that is deleted afterwards;
* **no network** (WASI preview 1 has no sockets) and no environment beyond ``PYTHONHOME``/``TMPDIR``;
* a **memory cap** (``memory_mb``): allocating more fails with ``MemoryError`` inside the guest;
* a **time limit** enforced by the runtime (epoch interruption), so even a tight loop that never
  returns to Python bytecode is stopped;
* **capped output**: stdout/stderr beyond the limit are dropped as they are written.

Only the Python standard library is available. The module is compiled once per process, in the
background, so the first request does not pay for it.
"""

from __future__ import annotations

import math
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from orchestrator.errors import ConfigurationError

_TICK_S = 0.05  # epoch granularity: the time limit is accurate to about this much


@dataclass(frozen=True)
class WasmResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool


def wasm_unavailable_reason(root: Path) -> str | None:
    """Why the WebAssembly sandbox cannot run from ``root``, or None if it can."""
    try:
        import wasmtime  # noqa: F401
    except ImportError:
        return "the wasmtime package is not installed (pip install '.[sandbox]')"
    if not (root / "python.wasm").is_file():
        return f"{root / 'python.wasm'} not found (run scripts/fetch_python_wasm.py)"
    if not any((root / "lib").glob("python3.*")):
        return f"{root / 'lib'} has no Python standard library"
    return None


class _Ticker:
    """One thread per engine advancing its epoch; each run sets a deadline in ticks."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wasm-epoch", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(_TICK_S):
            self._engine.increment_epoch()

    def stop(self) -> None:
        self._stop.set()


class WasmPython:
    def __init__(self, root: Path, *, memory_mb: int = 256, max_output_bytes: int = 256 * 1024) -> None:
        reason = wasm_unavailable_reason(root)
        if reason:
            raise ConfigurationError(f"WebAssembly Python sandbox unavailable: {reason}")
        import wasmtime

        self._wasmtime = wasmtime
        self.root = root
        self.memory_mb = memory_mb
        self._max_output = max_output_bytes
        config = wasmtime.Config()
        config.epoch_interruption = True
        self._engine = wasmtime.Engine(config)
        self._ticker = _Ticker(self._engine)
        self._module = None
        self._compile_error: Exception | None = None
        self._compiled = threading.Event()
        threading.Thread(target=self._compile, name="wasm-compile", daemon=True).start()

    def _compile(self) -> None:
        try:
            self._module = self._wasmtime.Module.from_file(self._engine, str(self.root / "python.wasm"))
        except Exception as exc:  # reported on first use and by health_check
            self._compile_error = exc
        finally:
            self._compiled.set()

    def ready(self, timeout: float | None = None) -> bool:
        """Wait for the background compile; True if the module is usable."""
        self._compiled.wait(timeout)
        return self._module is not None

    def run(self, argv: list[str], stdin: bytes, timeout_s: float) -> WasmResult:
        """Run ``python.wasm`` with ``argv``, ``stdin`` as its input. Blocking: call from a thread."""
        if not self.ready():
            raise ConfigurationError(f"could not compile python.wasm: {self._compile_error}")
        wt = self._wasmtime
        out, err = bytearray(), bytearray()

        def capped(buf: bytearray):
            def write(data: bytes) -> None:
                room = self._max_output - len(buf)
                if room > 0:
                    buf.extend(data[:room])
            return write

        with tempfile.TemporaryDirectory(prefix="orch_wasm_", ignore_cleanup_errors=True) as scratch:
            stdin_path = Path(scratch) / ".stdin"
            stdin_path.write_bytes(stdin)
            work = Path(scratch) / "tmp"
            work.mkdir()
            wasi = wt.WasiConfig()
            wasi.argv = argv
            wasi.env = [("PYTHONHOME", "/"), ("TMPDIR", "/tmp"), ("HOME", "/tmp")]
            wasi.preopen_dir(str(self.root / "lib"), "/lib", False)  # read-only
            wasi.preopen_dir(str(work), "/tmp", True)
            wasi.stdin_file = str(stdin_path)
            wasi.stdout_custom = capped(out)
            wasi.stderr_custom = capped(err)

            store = wt.Store(self._engine)
            try:
                store.set_wasi(wasi)
                store.set_limits(memory_size=self.memory_mb * 1024 * 1024)
                store.set_epoch_deadline(max(1, math.ceil(timeout_s / _TICK_S)))
                linker = wt.Linker(self._engine)
                linker.define_wasi()
                instance = linker.instantiate(store, self._module)
                exit_code: int | None = 0
                timed_out = False
                try:
                    instance.exports(store)["_start"](store)
                except wt.ExitTrap as exc:
                    exit_code = exc.code
                except wt.Trap as exc:
                    exit_code = None
                    timed_out = exc.trap_code == wt.TrapCode.INTERRUPT
                    if not timed_out:
                        err.extend(f"\nwasm trap: {exc.message}".encode()[: max(0, self._max_output - len(err))])
            finally:
                store.close()  # releases the preopened folders so they can be deleted
        return WasmResult(exit_code, bytes(out), bytes(err), timed_out)

    def close(self) -> None:
        self._ticker.stop()
