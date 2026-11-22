# Multi-Tool Orchestrator — production image.
# Build:  docker build -t multi-tool-orchestrator .
# Run:    docker compose up   (see docker-compose.yml and docs/DEPLOYMENT.md)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Unprivileged user; /data holds uploads, the audit log and durable state, /config the policy
# and principals.
RUN useradd --create-home --uid 10001 orch \
 && mkdir -p /data/uploads /data/audit /data/state /config \
 && chown -R orch:orch /data

# Dependencies first (cached layer), then the package without re-resolving dependencies.
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

# Python sandbox: CPython for WebAssembly, downloaded and checked against a pinned SHA-256.
COPY scripts/fetch_python_wasm.py ./scripts/
RUN python scripts/fetch_python_wasm.py --dest /opt/wasi

USER orch

ENV ORCH_ENVIRONMENT=production \
    ORCH_FILE_TOOL_ROOT=/data/uploads \
    ORCH_AUDIT_LOG_PATH=/data/audit/audit.jsonl \
    ORCH_STATE_PATH=/data/state/orchestrator.db \
    ORCH_POLICY_PATH=/config/policy.json \
    ORCH_PRINCIPALS_PATH=/config/principals.json \
    ORCH_PYTHON_SANDBOX=wasm \
    ORCH_PYTHON_WASM_DIR=/opt/wasi/python-3.15.2

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "orchestrator", "serve", "--host", "0.0.0.0", "--port", "8000"]
