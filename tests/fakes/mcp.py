"""Fake service-layer call recorders for MCP tool contract tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pitwall.mcp.registry import TOOL_NAMES


@dataclass
class MCPToolCall:
    """Record of a single MCP tool invocation.

    Attributes:
        tool_name: The name of the MCP tool that was invoked (e.g. "pitwall_list_capabilities").
        arguments: The keyword arguments passed to the tool handler.
        result: The return value from the tool handler (None if not yet returned).
        error: Any exception raised by the tool handler (None if successful).
        called_at: Timestamp when the call was made.
    """

    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: Exception | None = None
    called_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FakeServiceLayerRecorder:
    """Records MCP tool invocations for hermetic contract testing.

    Wraps the ``TOOL_REGISTRY`` handlers to intercept calls and record
    arguments, results, and errors without hitting live services.

    Example::

        recorder = FakeServiceLayerRecorder()
        recorder.install()

        # Simulate calling a tool
        result = await recorder.call_tool("pitwall_list_capabilities", capability_class="embedding")

        assert recorder.calls[0].tool_name == "pitwall_list_capabilities"
        assert recorder.calls[0].arguments == {"capability_class": "embedding"}
        assert "capabilities" in recorder.calls[0].result

        recorder.uninstall()

    For use as a pytest fixture, see ``fake_mcp_recorder``.
    """

    def __init__(self) -> None:
        self._original_handlers: dict[str, Any] = {}
        self.calls: list[MCPToolCall] = []
        self._installed = False

    def _wrap_handler(self, tool_name: str, original_handler: Any) -> Any:
        """Wrap a tool handler to record invocations."""

        async def wrapped(**kwargs: Any) -> dict[str, Any]:
            call = MCPToolCall(tool_name=tool_name, arguments=kwargs)
            self.calls.append(call)
            try:
                result = await original_handler(**kwargs)
                call.result = result
                return result
            except (
                Exception
            ) as exc:  # reason: recorder captures any handler exception before re-raising
                call.error = exc
                raise

        return wrapped

    def install(self) -> None:
        """Replace TOOL_REGISTRY handlers with wrapped versions that record calls."""
        if self._installed:
            return

        from pitwall.mcp import registry as registry_module

        for spec in registry_module.TOOL_REGISTRY:
            self._original_handlers[spec.name] = spec.handler
            spec.handler = self._wrap_handler(spec.name, spec.handler)
            if hasattr(spec.handler, "__name__"):
                pass

        self._installed = True

    def uninstall(self) -> None:
        """Restore original handlers."""
        if not self._installed:
            return

        from pitwall.mcp import registry as registry_module

        for spec in registry_module.TOOL_REGISTRY:
            if spec.name in self._original_handlers:
                spec.handler = self._original_handlers[spec.name]

        self._original_handlers.clear()
        self._installed = False

    async def call_tool(
        self,
        tool_name: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call a tool by name and record the invocation.

        Args:
            tool_name: One of the ``TOOL_NAMES``.
            **kwargs: Arguments to pass to the tool handler.

        Returns:
            The result dict from the tool handler.

        Raises:
            ValueError: If tool_name is not in TOOL_NAMES.
        """
        if tool_name not in TOOL_NAMES:
            raise ValueError(f"Unknown tool: {tool_name!r}")

        from pitwall.mcp import registry as registry_module

        by_name = {spec.name: spec for spec in registry_module.TOOL_REGISTRY}
        spec = by_name[tool_name]

        call = MCPToolCall(tool_name=tool_name, arguments=kwargs)
        self.calls.append(call)
        try:
            result = await spec.handler(**kwargs)
            call.result = result
            return result
        except (
            Exception
        ) as exc:  # reason: recorder captures any handler exception before re-raising
            call.error = exc
            raise

    def get_calls(
        self,
        tool_name: str | None = None,
    ) -> list[MCPToolCall]:
        """Return recorded calls, optionally filtered by tool name.

        Args:
            tool_name: If provided, only return calls for this tool.

        Returns:
            List of recorded calls in chronological order.
        """
        if tool_name is None:
            return list(self.calls)
        return [c for c in self.calls if c.tool_name == tool_name]

    def assert_called(
        self,
        tool_name: str,
        *,
        times: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Assert that a tool was called with specific arguments.

        Args:
            tool_name: The tool name to check.
            times: If provided, assert the tool was called exactly this many times.
            **kwargs: If provided, assert these keyword arguments were passed.
        """
        matching = self.get_calls(tool_name)
        if kwargs:
            matching = [c for c in matching if c.arguments == kwargs]

        if times is not None:
            assert len(matching) == times, (
                f"Expected {tool_name} to be called {times} times, "
                f"but it was called {len(matching)} times"
            )
        else:
            assert len(matching) > 0, f"Expected {tool_name} to be called, but it was not called"

    def reset(self) -> None:
        """Clear all recorded calls."""
        self.calls.clear()
