"""Shared pytest fixtures for MCP transport contract tests."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters


_REPO_ROOT_ARG = "--repo-root"


def _repo_root() -> str:
    for i, arg in enumerate(sys.argv):
        if arg.startswith(f"{_REPO_ROOT_ARG}="):
            return arg.split("=", 1)[1]
        if arg == _REPO_ROOT_ARG and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return str(Path(__file__).resolve().parent.parent.parent)


@pytest.fixture()
def repo_root() -> str:
    return _repo_root()


@pytest.fixture()
def stdio_server_params(repo_root: str) -> StdioServerParameters:
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pitwall.mcp"],
        env={
            "PITWALL_MCP_TRANSPORT": "stdio",
            "RUNPOD_API_KEY": "test-key",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "REDIS_URL": "redis://localhost:6379/0",
        },
        cwd=repo_root,
    )


@pytest.fixture
async def stdio_mcp_session(
    stdio_server_params: StdioServerParameters,
) -> AsyncGenerator[ClientSession, None]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    async with (
        stdio_client(stdio_server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session
