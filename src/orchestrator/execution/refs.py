"""Resolve ``{"$ref": "<task_id>.<path>"}`` markers against upstream task outputs, and
``{"$ref": "@R1.<task_id>.<path>"}`` against outputs of earlier requests in the conversation.

Paths walk dict keys and list indices (``load.rows.0.Revenue``). Resolution produces a new
structure; upstream outputs are deep-copied so a downstream tool cannot mutate them.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from orchestrator.contracts.plan import REF_KEY, InputRef
from orchestrator.errors import InputResolutionError


def lookup(ref: InputRef, outputs: Mapping[str, Any],
           context: Mapping[str, Mapping[str, Any]] | None = None) -> Any:
    if ref.context is not None:
        if context is None or ref.context not in context:
            raise InputResolutionError(f"$ref to earlier request {ref.context!r}, which is not in this conversation",
                                       details={"ref": str(ref)})
        outputs = context[ref.context]
    head = f"@{ref.context}.{ref.task_id}" if ref.context else ref.task_id
    if ref.task_id not in outputs:
        raise InputResolutionError(
            f"$ref to {head!r}, which has no output", details={"ref": head}
        )
    value: Any = outputs[ref.task_id]
    for i, part in enumerate(ref.path):
        where = ".".join((head, *ref.path[: i + 1]))
        if isinstance(value, Mapping):
            if part not in value:
                raise InputResolutionError(f"$ref {where!r} not found", details={"ref": where})
            value = value[part]
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                raise InputResolutionError(f"$ref {where!r}: bad list index", details={"ref": where}) from None
        else:
            raise InputResolutionError(f"$ref {where!r}: cannot index into {type(value).__name__}", details={"ref": where})
    return copy.deepcopy(value)


def resolve(value: Any, outputs: Mapping[str, Any], context: Mapping[str, Mapping[str, Any]] | None = None) -> Any:
    if isinstance(value, dict):
        if set(value) == {REF_KEY} and isinstance(value[REF_KEY], str):
            return lookup(InputRef.parse(value[REF_KEY]), outputs, context)
        return {k: resolve(v, outputs, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs, context) for v in value]
    return value
