"""Prove MCP stdio transport: initialize + list-tools works via the Python mcp SDK."""

from __future__ import annotations

import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.anyio

_REPO_ROOT_ARG = "--repo-root"


def _repo_root() -> str:
    for i, arg in enumerate(sys.argv):
        if arg.startswith(f"{_REPO_ROOT_ARG}="):
            return arg.split("=", 1)[1]
        if arg == _REPO_ROOT_ARG and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent.parent)


@pytest.fixture()
def server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pitwall.mcp"],
        env={
            "PITWALL_MCP_TRANSPORT": "stdio",
            "RUNPOD_API_KEY": "test-key",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "REDIS_URL": "redis://localhost:6379/0",
        },
        cwd=_repo_root(),
    )


async def test_initialize_returns_server_info(server_params: StdioServerParameters) -> None:
    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        result = await session.initialize()
        assert result.serverInfo.name == "pitwall"
        assert result.protocolVersion


async def test_list_tools_returns_pitwall_health(server_params: StdioServerParameters) -> None:
    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
        tool_names = [t.name for t in result.tools]
        assert "pitwall_health" in tool_names


async def test_initialize_capabilities_declare_tools(
    server_params: StdioServerParameters,
) -> None:
    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        result = await session.initialize()
        assert result.capabilities.tools is not None
