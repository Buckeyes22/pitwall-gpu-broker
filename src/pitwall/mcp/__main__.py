"""Entry-point for ``python -m pitwall.mcp``.

The public alpha permits only local stdio. Network transports fail closed until
an authenticated HTTP transport is implemented and reviewed.
"""

from __future__ import annotations

import os

from pitwall.mcp import mcp


def main() -> None:
    from pitwall.mcp import ensure_runtime_env

    ensure_runtime_env()
    transport = os.environ.get("PITWALL_MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        raise SystemExit(
            "network MCP transports are unavailable in the public alpha; "
            "set PITWALL_MCP_TRANSPORT=stdio"
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
