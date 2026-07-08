"""Error adapter — map service exceptions to structured MCP errors.

This module converts service-layer exceptions (``PitwallApiError``,
``ResolverError``, ``LeaseTransitionError``) into MCP ``McpError`` responses
so that MCP clients receive the same structured error codes that the REST API
uses.

Public API::

    from pitwall.mcp.error_adapter import adapt_error

    try:
        result = some_service_function()
    except Exception as e:
        raise adapt_error(e) from e

The ``McpError.error.data`` field contains a dict with at least::

    {
        "error": "<error_code>",       # same string code as REST API
        ...                             # additional fields from the exception
    }

MCP integer codes are in the ``-32000`` range (JSON-RPC reserved).
All pitwall service errors use ``-32000`` as the base code with the
specific string error code carried in ``error.data["error"]``.
"""

from __future__ import annotations

from typing import Any, cast

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

PITWALL_ERROR_CODE_BASE = -32000

_API_ERROR_CODE_TO_MCP_CODE: dict[str, int] = {}


def _extract_error_data(exc: Exception) -> dict[str, Any]:
    """Extract structured error data from a service exception.

    Returns a dict with at least ``{"error": "<code>"}``.
    Subclasses may add more fields (e.g. ``name``, ``id``).
    """
    if hasattr(exc, "to_response_body"):
        return cast(dict[str, Any], exc.to_response_body())
    if hasattr(exc, "to_dict"):
        return cast(dict[str, Any], exc.to_dict())
    if hasattr(exc, "error_code"):
        return {"error": exc.error_code}
    return {"error": "internal_error"}


def _get_error_code(exc: Exception) -> str:
    """Return the error code string for an exception."""
    if hasattr(exc, "error_code"):
        return cast(str, exc.error_code)
    return "internal_error"


def adapt_error(exc: Exception) -> McpError:
    """Convert a service exception to an ``McpError`` with structured error data.

    Args:
        exc: Any exception, typically a ``PitwallApiError``,
            ``ResolverError``, or ``LeaseTransitionError``.

    Returns:
        An ``McpError`` with ``ErrorData`` containing the same
        ``error_code`` string used by the REST API.
    """
    error_code = _get_error_code(exc)
    data = _extract_error_data(exc)
    mcp_code = _API_ERROR_CODE_TO_MCP_CODE.get(error_code, PITWALL_ERROR_CODE_BASE)
    message = str(exc) or error_code
    error_data = ErrorData(code=mcp_code, message=message, data=data)
    return McpError(error_data)


def register_error_code(error_code: str, mcp_code: int) -> None:
    """Register a mapping from a string error code to an MCP integer code.

    Args:
        error_code: The string error code (e.g. ``"capability_not_found"``).
        mcp_code: The MCP integer code in the ``-32000`` range.
    """
    _API_ERROR_CODE_TO_MCP_CODE[error_code] = mcp_code


__all__ = [
    "adapt_error",
    "register_error_code",
    "PITWALL_ERROR_CODE_BASE",
]
