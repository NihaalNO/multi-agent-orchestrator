"""Local development server: auto-reload, a ready-to-use dev user, and your .env loaded.

    .venv\\Scripts\\python scripts\\dev_server.py [--port 8000]

What it does (nothing here is for production):

* Loads ``.env`` from the project root into the environment, without overriding variables that
  are already set and without printing any values. ``python -m orchestrator serve`` does not read
  ``.env``; Docker Compose does.
* Fills in local defaults for anything not configured:
    - policy:     config/policy.example.json (per-user folders, shared read-only area)
    - principals: config/dev/principals.json, created on first run with one user, "dev", holding
                  the analyst, admin and approver roles
    - API key:    config/dev/api-key.txt (generated once; only its hash is in principals.json)
    - state:      data/dev/state.db        audit log: data/dev/audit.jsonl
    - uploads:    data/uploads
  ``config/dev/`` and ``data/`` are git-ignored.
* Restarts the server whenever a ``.py`` file under ``src/`` changes.

Why not ``uvicorn --reload``: on Windows its reloader stops the old worker with a console Ctrl+C
event and then waits for it. A server started without a console window (by an IDE or launcher)
never receives that event, so the reload hangs and the old code keeps serving. Restarting the
whole server process works everywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_CONFIG = ROOT / "config" / "dev"
KEY_FILE = DEV_CONFIG / "api-key.txt"
PRINCIPALS = DEV_CONFIG / "principals.json"


def load_dotenv(path: Path) -> list[str]:
    """Minimal KEY=VALUE reader. Returns the names it set (never the values)."""
    loaded = []
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def ensure_dev_user() -> bool:
    """Create the dev principal and key on first run. Returns True if it was just created."""
    if PRINCIPALS.exists() and KEY_FILE.exists():
        return False
    sys.path.insert(0, str(ROOT / "src"))
    from orchestrator.api.auth import hash_api_key

    DEV_CONFIG.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    KEY_FILE.write_text(key + "\n", encoding="utf-8")
    PRINCIPALS.write_text(json.dumps({"principals": [{
        "user_id": "dev", "roles": ["analyst", "admin", "approver"], "api_key_sha256": hash_api_key(key),
    }]}, indent=2) + "\n", encoding="utf-8")
    return True


def ensure_receipt_key() -> Path:
    """A local Ed25519 key for signing receipts (gitignored, like the dev API key)."""
    path = DEV_CONFIG / "receipt-key.pem"
    if not path.exists():
        sys.path.insert(0, str(ROOT / "src"))
        from orchestrator.receipts import generate_key

        generate_key(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the orchestrator API for local development.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    loaded = load_dotenv(ROOT / ".env")
    created = ensure_dev_user()
    defaults = {
        "ORCH_POLICY_PATH": str(ROOT / "config" / "policy.example.json"),
        "ORCH_PRINCIPALS_PATH": str(PRINCIPALS),
        "ORCH_STATE_PATH": str(ROOT / "data" / "dev" / "state.db"),
        "ORCH_AUDIT_LOG_PATH": str(ROOT / "data" / "dev" / "audit.jsonl"),
        "ORCH_FILE_TOOL_ROOT": str(ROOT / "data" / "uploads"),
        "ORCH_RECEIPT_KEY_PATH": str(ensure_receipt_key()),
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)

    say(f"loaded from .env: {', '.join(loaded) or '(nothing)'}")
    say(f"planner: {os.environ.get('ORCH_LLM_PROVIDER', 'anthropic')}  "
        f"search: {os.environ.get('ORCH_SEARCH_BACKEND', 'none')}")
    say(f"{'created' if created else 'using'} dev user 'dev'; API key in {KEY_FILE.relative_to(ROOT)}")
    say(f"API docs: http://127.0.0.1:{args.port}/docs")
    return serve_with_restarts(args.port)


def say(message: str) -> None:
    print(f"[dev] {message}", flush=True)


def _snapshot() -> dict[Path, float]:
    return {p: p.stat().st_mtime for p in (ROOT / "src").rglob("*.py")}


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()  # TerminateProcess on Windows, SIGTERM (graceful shutdown) elsewhere
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def serve_with_restarts(port: int) -> int:
    cmd = [sys.executable, "-m", "uvicorn", "orchestrator.api:create_app", "--factory",
           "--host", "127.0.0.1", "--port", str(port)]
    seen = _snapshot()
    proc = subprocess.Popen(cmd, cwd=ROOT)
    exit_reported = False
    say(f"watching src/ for changes (server pid {proc.pid})")
    try:
        while True:
            time.sleep(1)
            now = _snapshot()
            if now != seen:
                changed = sorted({p for p in set(now) | set(seen) if now.get(p) != seen.get(p)})
                names = ", ".join(str(p.relative_to(ROOT)) for p in changed[:3]) + (" ..." if len(changed) > 3 else "")
                say(f"change in {names}; restarting")
                seen = now
                _stop(proc)
                proc = subprocess.Popen(cmd, cwd=ROOT)
                exit_reported = False
                say(f"server restarted (pid {proc.pid})")
            elif proc.poll() is not None and not exit_reported:
                say(f"server exited with code {proc.returncode}; fix the error, it restarts on the next change")
                exit_reported = True
    except KeyboardInterrupt:
        pass
    finally:
        _stop(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
