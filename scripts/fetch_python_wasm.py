"""Download the CPython WebAssembly (WASI) build used by the Python sandbox, and verify it.

    python scripts/fetch_python_wasm.py [--dest data/wasi]

Source: https://github.com/brettcannon/cpython-wasi-build (CPython built for WASI by a CPython core
developer; "unofficial" builds). The archive is checked against a pinned SHA-256 before anything is
unpacked, so a changed or tampered file is rejected. Point ORCH_PYTHON_WASM_DIR at the result.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

VERSION = "3.15.2"
ASSET = f"python-{VERSION}-wasi_sdk-33.zip"
URL = f"https://github.com/brettcannon/cpython-wasi-build/releases/download/v{VERSION}/{ASSET}"
SHA256 = "b8a63559c56760bbdc310dedb74fd69d10bcbafae26b705bea0ad353ad256888"
MAX_BYTES = 64 * 1024 * 1024


def fetch(dest: Path) -> Path:
    target = dest / f"python-{VERSION}"
    if (target / "python.wasm").is_file():
        print(f"already present: {target}")
        return target
    print(f"downloading {URL}")
    with urllib.request.urlopen(URL, timeout=120) as resp:  # noqa: S310 - fixed https URL
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise SystemExit("download is larger than expected; refusing it")
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"SHA-256 mismatch: expected {SHA256}, got {digest}; refusing it")
    staging = dest / f".python-{VERSION}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            path = (staging / member.filename).resolve()
            if not path.is_relative_to(staging.resolve()):
                raise SystemExit(f"unsafe path in archive: {member.filename}")
        zf.extractall(staging)
    wasm = next(staging.rglob("python.wasm"), None)
    if wasm is None:
        raise SystemExit("archive has no python.wasm")
    shutil.rmtree(target, ignore_errors=True)
    _move(wasm.parent, target)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"verified and unpacked to {target}")
    return target


def _move(src: Path, dst: Path, attempts: int = 10) -> None:
    """Rename, retrying briefly: on Windows a virus scanner may still hold freshly unpacked files."""
    for i in range(attempts):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.5 * (i + 1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=Path("data/wasi"))
    args = parser.parse_args()
    args.dest.mkdir(parents=True, exist_ok=True)
    fetch(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
