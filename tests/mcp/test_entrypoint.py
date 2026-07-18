"""Security boundary tests for the MCP process entry point."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_entrypoint_runs_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    from pitwall.mcp import __main__ as entrypoint

    monkeypatch.setenv("PITWALL_MCP_TRANSPORT", "stdio")
    with (
        patch("pitwall.mcp.ensure_runtime_env"),
        patch.object(entrypoint.mcp, "run") as run,
    ):
        entrypoint.main()

    run.assert_called_once_with(transport="stdio")


@pytest.mark.parametrize("transport", ["sse", "streamable-http", "bogus"])
def test_entrypoint_rejects_every_non_stdio_transport(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    from pitwall.mcp import __main__ as entrypoint

    monkeypatch.setenv("PITWALL_MCP_TRANSPORT", transport)
    with (
        patch("pitwall.mcp.ensure_runtime_env"),
        pytest.raises(SystemExit, match="network MCP transports are unavailable"),
    ):
        entrypoint.main()
