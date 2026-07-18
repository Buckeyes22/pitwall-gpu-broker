"""Schema adapter — convert REST Pydantic request models into MCP-compatible JSON Schema.

The MCP tool ``inputSchema`` is a standard JSON Schema ``object``.  This module
derives that schema from the *existing* Pydantic request models used by the REST
API surface, so model definitions are authored in exactly one place.

Public API::

    from pitwall.mcp.schema_adapter import pydantic_to_mcp_schema
    schema = pydantic_to_mcp_schema(LeaseCreate)

The returned dict is ready to pass as the ``inputSchema`` field of an MCP tool
definition.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from pydantic import BaseModel

_DEFNITION_PREFIX = "$defs"
_REF_KEY = "$ref"
_STRIP_KEYS: frozenset[str] = frozenset({"title", "additionalProperties"})
_KEEP_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"type", "properties", "required"})


def pydantic_to_mcp_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic model class to an MCP-compatible JSON Schema dict.

    The function calls ``model_json_schema(by_alias=True)`` on *model_cls*
    and then post-processes the result:

    * Inlines any ``$defs`` / ``$ref`` references so the output is a single
      self-contained JSON Schema object.
    * Strips Pydantic-internal noise keys (``title``, ``additionalProperties``).
    * Preserves descriptions, constraints, defaults, and enum values.

    Args:
        model_cls: A Pydantic v2 ``BaseModel`` subclass.

    Returns:
        A JSON Schema dict suitable for use as an MCP tool ``inputSchema``.
    """
    raw: dict[str, Any] = model_cls.model_json_schema(by_alias=True)
    defs: dict[str, Any] = raw.pop(_DEFNITION_PREFIX, {})
    resolved = _resolve_refs(raw, defs)
    cleaned = _strip_keys(resolved, _STRIP_KEYS, _KEEP_TOP_LEVEL_KEYS)
    return cast(dict[str, Any], cleaned)


def _resolve_refs(
    node: Any,
    defs: dict[str, Any],
) -> Any:
    """Recursively resolve ``$ref`` pointers against *defs*, inlining them.

    Any referenced definition is deep-copied before inlining so that shared
    references don't alias each other.
    """
    if isinstance(node, dict):
        if _REF_KEY in node:
            ref_path: str = node[_REF_KEY]
            defn_name = ref_path.rsplit("/", 1)[-1]
            if defn_name in defs:
                resolved = _resolve_refs(deepcopy(defs[defn_name]), defs)
                if len(node) > 1:
                    merged = {k: v for k, v in node.items() if k != _REF_KEY}
                    merged.update(resolved)
                    return merged
                return resolved
            return node
        return {k: _resolve_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, defs) for item in node]
    return node


def _strip_keys(
    node: Any,
    strip: frozenset[str],
    keep_at_top: frozenset[str] | None = None,
    depth: int = 0,
) -> Any:
    """Remove noise keys from the schema tree.

    * At the top level (``depth == 0``), only keys in *keep_at_top* plus
      non-stripped keys are kept.
    * At nested levels, keys in *strip* are removed.
    """
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for k, v in node.items():
            if depth == 0 and keep_at_top is not None:
                if k not in keep_at_top and k in strip:
                    continue
            elif k in strip:
                continue
            result[k] = _strip_keys(v, strip, keep_at_top, depth + 1)
        return result
    if isinstance(node, list):
        return [_strip_keys(item, strip, keep_at_top, depth) for item in node]
    return node


__all__ = ["pydantic_to_mcp_schema"]
