"""Configuration management.

Settings are read from ``ORCH_*`` environment variables. Secrets (API keys, admin token) are
held as ``SecretStr`` so they never appear in ``repr()``, logs or audit events.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from orchestrator.errors import ConfigurationError

ENV_PREFIX = "ORCH_"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str = "development"

    # Execution limits
    max_concurrency: int = Field(default=8, ge=1, le=256)
    max_retries_cap: int = Field(
        default=3, ge=0, le=10, description="Hard ceiling applied over any tool's own retry setting."
    )
    health_check_timeout_s: float = Field(default=5.0, gt=0, le=60)
    retry_base_delay_s: float = Field(default=0.2, ge=0, le=30)
    retry_max_delay_s: float = Field(default=5.0, ge=0, le=300)
    idempotency_ttl_s: int = Field(default=24 * 3600, ge=60)
    execution_deadline_s: float | None = Field(default=None, gt=0, description="Whole-plan time limit.")

    # Tools
    file_tool_root: Path = Path("data/uploads")
    python_exec_timeout_s: float = Field(default=10.0, gt=0, le=120)
    # Python sandbox. "auto": WebAssembly when wasmtime and python.wasm are available, otherwise a
    # plain subprocess (not a security boundary). "wasm": refuse to start without the sandbox.
    python_sandbox: Literal["auto", "wasm", "subprocess"] = "auto"
    python_wasm_dir: Path = Path("data/wasi/python-3.15.2")
    python_memory_mb: int = Field(default=256, ge=16, le=4096)
    # Uploads (PUT /files). Default cap = the file tool's read cap: a bigger file could not be read.
    upload_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)
    upload_extensions: str = Field(default="csv,txt,md,json", description="Comma-separated accepted file types.")
    # "none": no web search tool is registered. "anthropic": Claude's server-side web search.
    # "gemini": Gemini with Google Search grounding. Both are billed per search by their provider.
    # "tavily": Tavily's search API (key in TAVILY_API_KEY), 1 credit per basic search.
    search_backend: Literal["none", "anthropic", "gemini", "tavily"] = "none"
    tavily_search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = Field(
        default="basic", description="Tavily search depth; advanced costs 2 credits per search.")
    search_model: str | None = Field(default=None, description="Model for web search; unset → provider default.")
    search_max_uses: int = Field(default=3, ge=1, le=10, description="Web searches Claude may run per call.")

    # Planner: "anthropic" (Claude), "gemini" (Google Gemini), "groq", "ollama" or "rule_based"
    # (offline, deterministic).
    llm_provider: str = "anthropic"
    llm_model: str | None = Field(default=None, description="Planner model; unset → provider default.")
    llm_base_url: str | None = Field(default=None, description="Groq/Ollama endpoint override, e.g. a remote Ollama.")
    max_replans: int = Field(
        default=1, ge=0, le=3,
        description="How often a failed run may be re-planned with the error as feedback (LLM planners only; "
                    "each costs one more planner call). 0 disables.",
    )
    llm_max_output_tokens: int = Field(
        default=4096, ge=256, le=131072,
        description="Groq/Ollama output budget per plan, reasoning included. Groq counts it against the "
                    "account's tokens-per-minute limit, so keep prompt + budget under that limit.",
    )
    llm_json_mode: Literal["auto", "json_schema", "json_object"] = Field(
        default="auto", description="Groq/Ollama structured-output mode; auto picks per provider and model."
    )
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"  # Anthropic only
    # Optional. For the planner's provider (and web search, when it uses the same provider).
    # If unset, each SDK reads its own variables: ANTHROPIC_API_KEY / `ant auth login`, or
    # GEMINI_API_KEY / GOOGLE_API_KEY.
    llm_api_key: SecretStr | None = None

    # Permissions. Unset policy → no grants → everything denied (default deny).
    # Unset principals → no API key authenticates → every protected endpoint returns 401.
    policy_path: Path | None = None
    principals_path: Path | None = None
    admin_role: str = "admin"
    approver_role: str = "approver"
    approval_ttl_s: int = Field(default=3600, ge=60, le=7 * 24 * 3600)

    # Durable state (requests, approvals, idempotency) in SQLite. Unset → in memory, lost on restart.
    state_path: Path | None = None

    # Observability
    audit_log_path: Path | None = None
    audit_to_log: bool = Field(default=False, description="Also write audit events to the application log.")
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        environ = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            raw = environ.get(f"{ENV_PREFIX}{name.upper()}")
            if raw is not None and raw != "":
                values[name] = raw
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ConfigurationError(
                "Invalid orchestrator configuration",
                details={"errors": exc.errors(include_url=False, include_input=False)},
            ) from exc

    @property
    def upload_extension_set(self) -> frozenset[str]:
        return frozenset("." + e.strip().lower().lstrip(".") for e in self.upload_extensions.split(",") if e.strip())

    @property
    def planner_model(self) -> str | None:
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider.lower())

    @property
    def effective_search_model(self) -> str | None:
        return self.search_model or DEFAULT_MODELS.get(self.search_backend)

    def api_key_for(self, provider: str) -> str | None:
        """``llm_api_key`` belongs to the planner's provider; other providers use their SDK's env vars."""
        if self.llm_api_key and provider == self.llm_provider.lower():
            return self.llm_api_key.get_secret_value()
        return None


# Default model per provider when ORCH_LLM_MODEL / ORCH_SEARCH_MODEL are unset.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "gemini": "gemini-3.8-flash",
    "groq": "openai/gpt-oss-120b",
    "ollama": "gpt-oss:20b",
}
