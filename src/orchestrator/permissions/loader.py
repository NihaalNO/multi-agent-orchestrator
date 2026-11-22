"""Load the grant policy and API-key principals from JSON files.

Policy file (``ORCH_POLICY_PATH``)::

    {"grants": [{"subject": "role:analyst", "tool_id": "web_search", "max_access": "read"}, ...]}

Principals file (``ORCH_PRINCIPALS_PATH``)::

    {"principals": [{"user_id": "alice", "roles": ["analyst"], "api_key_sha256": "<hex>"}]}

Only SHA-256 hashes of API keys are stored. Generate one with::

    python -c "import hashlib,getpass; print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.contracts import PermissionGrant
from orchestrator.errors import ConfigurationError


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: list[PermissionGrant] = Field(default_factory=list)


class PrincipalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(pattern=r"^[A-Za-z0-9_.@-]+$")
    roles: list[str] = Field(default_factory=list)
    api_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PrincipalsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principals: list[PrincipalRecord] = Field(default_factory=list)


def _load(path: Path, model: type[BaseModel]) -> BaseModel:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(raw)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid JSON: {exc}") from exc
    except ValidationError as exc:
        raise ConfigurationError(
            f"{path} is invalid", details={"errors": exc.errors(include_url=False, include_input=False)}
        ) from exc


def load_grants(path: Path) -> list[PermissionGrant]:
    return _load(path, PolicyFile).grants  # type: ignore[attr-defined]


def load_principals(path: Path) -> list[PrincipalRecord]:
    records: list[PrincipalRecord] = _load(path, PrincipalsFile).principals  # type: ignore[attr-defined]
    ids = [r.user_id for r in records]
    hashes = [r.api_key_sha256 for r in records]
    if len(set(ids)) != len(ids) or len(set(hashes)) != len(hashes):
        raise ConfigurationError(f"{path}: duplicate user_id or API key hash")
    return records
