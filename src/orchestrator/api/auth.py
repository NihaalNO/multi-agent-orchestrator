"""API-key authentication.

Clients send ``Authorization: Bearer <api key>``. Only SHA-256 hashes of keys are held; the
presented key is hashed and compared in constant time against every stored hash. The result
is a ``Principal`` whose roles come from the principals file — never from the request.

JWT / OAuth 2.0 (spec §4) can replace this behind the same ``authenticate`` interface.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Iterable

from orchestrator.contracts import Principal
from orchestrator.permissions.loader import PrincipalRecord


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeyAuthenticator:
    def __init__(self, records: Iterable[PrincipalRecord] = ()) -> None:
        self._records = [
            (bytes.fromhex(r.api_key_sha256), Principal(user_id=r.user_id, roles=frozenset(r.roles)))
            for r in records
        ]

    def authenticate(self, authorization: str | None) -> Principal | None:
        if not authorization:
            return None
        scheme, _, key = authorization.partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            return None
        presented = hashlib.sha256(key.strip().encode("utf-8")).digest()
        found: Principal | None = None
        for stored, principal in self._records:  # no early exit: constant work per request
            if hmac.compare_digest(presented, stored):
                found = principal
        return found
