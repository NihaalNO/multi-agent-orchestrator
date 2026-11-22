"""Resolve ``{"$ref": "<task_id>.<path>"}`` markers against upstream task outputs.

Paths walk dict keys and list indices (``load.rows.0.Revenue``). Resolution produces a new
structure; upstream outputs are deep-copied so a downstream tool cannot mutate them.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from orchestrator.contracts.plan import REF_KEY, InputRef
from orchestrator.errors import InputResolutionError


def lookup(ref: InputRef, outputs: Mapping[str, Any]) -> Any:
    if ref.task_id not in outputs:
        raise InputResolutionError(
            f"$ref to {ref.task_id!r}, which has no output", details={"ref": ref.task_id}
        )
    value: Any = outputs[ref.task_id]
    for i, part in enumerate(ref.path):
        where = ".".join((ref.task_id, *ref.path[: i + 1]))
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


def resolve(value: Any, outputs: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {REF_KEY} and isinstance(value[REF_KEY], str):
            return lookup(InputRef.parse(value[REF_KEY]), outputs)
        return {k: resolve(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs) for v in value]
    return value
