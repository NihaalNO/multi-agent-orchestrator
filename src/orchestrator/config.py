"""Configuration management.

Settings are read from ``ORCH_*`` environment variables. Secrets (API keys, admin token) are
held as ``SecretStr`` so they never appear in ``repr()``, logs or audit events.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from orchestrator.errors import ConfigurationError

ENV_PREFIX = "ORCH_"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str = "development"

    # Execution limits
    max_concurrency: int = Field(default=8, ge=1, le=256)
    default_tool_timeout_s: float = Field(default=30.0, gt=0, le=600)
    max_retries_cap: int = Field(
        default=3, ge=0, le=10, description="Hard ceiling applied over any tool's own retry setting."
    )
    health_check_timeout_s: float = Field(default=5.0, gt=0, le=60)

    # Tools
    file_tool_root: Path = Path("data/uploads")
    python_exec_timeout_s: float = Field(default=10.0, gt=0, le=120)

    # LLM (Phase 03) — provider is an open decision; nothing here is required yet.
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None

    # Admin API stopgap until principal-based auth (Phase 04). Unset → admin endpoints disabled.
    admin_token: SecretStr | None = None

    # Observability
    audit_log_path: Path | None = None

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
