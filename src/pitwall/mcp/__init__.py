"""Pitwall MCP server — local stdio entrypoint using the ``mcp`` Python SDK."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from pitwall.config import require_runtime_env
from pitwall.mcp.registry import register_all
from pitwall.security.redaction import configure_logging_redaction

configure_logging_redaction()
mcp = FastMCP("pitwall")


def ensure_runtime_env() -> None:
    """Validate required runtime env for the MCP service.

    Called from the serve entry points only, never at import, so test
    collection and ``import pitwall.mcp`` stay hermetic (no SystemExit at
    import time).
    """
    require_runtime_env("mcp")


@mcp.tool()
def pitwall_health() -> dict[str, str]:
    """Return Pitwall MCP server health status."""
    return {"ok": "true", "backend": "runpod"}


register_all(mcp)


__all__ = ["ensure_runtime_env", "mcp"]
