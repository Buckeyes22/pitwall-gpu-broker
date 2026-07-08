"""Tests for the MCP tool registry table."""

from __future__ import annotations

import pytest

from pitwall.mcp.registry import TOOL_NAMES, TOOL_REGISTRY, register_all


class TestToolRegistryStructure:
    def test_registry_has_exactly_23_entries(self) -> None:
        assert len(TOOL_REGISTRY) == 23

    def test_tool_names_count_is_23(self) -> None:
        assert len(TOOL_NAMES) == 23

    def test_all_names_start_with_pitwall_prefix(self) -> None:
        for spec in TOOL_REGISTRY:
            assert spec.name.startswith("pitwall_"), f"{spec.name} lacks pitwall_ prefix"

    def test_all_names_are_in_tool_names_set(self) -> None:
        for spec in TOOL_REGISTRY:
            assert spec.name in TOOL_NAMES

    def test_no_duplicate_names(self) -> None:
        names = [spec.name for spec in TOOL_REGISTRY]
        assert len(names) == len(set(names))

    def test_every_spec_has_nonempty_description(self) -> None:
        for spec in TOOL_REGISTRY:
            assert spec.description, f"{spec.name} has empty description"

    def test_every_spec_has_callable_handler(self) -> None:
        for spec in TOOL_REGISTRY:
            assert callable(spec.handler), f"{spec.name} handler is not callable"

    def test_tool_spec_is_frozen(self) -> None:
        spec = TOOL_REGISTRY[0]
        with pytest.raises(AttributeError):
            spec.name = "x"


class TestToolRegistryDiscoveryGroup:
    NAMES = {
        "pitwall_list_capabilities",
        "pitwall_describe_capability",
        "pitwall_list_providers",
        "pitwall_get_provider_health",
    }

    def test_discovery_tools_present(self) -> None:
        assert self.NAMES.issubset(TOOL_NAMES)


class TestToolRegistryInferenceGroup:
    NAMES = {
        "pitwall_submit_inference",
        "pitwall_submit_job",
        "pitwall_get_job_status",
        "pitwall_get_job_result",
        "pitwall_cancel_job",
    }

    def test_inference_tools_present(self) -> None:
        assert self.NAMES.issubset(TOOL_NAMES)


class TestToolRegistryLeaseGroup:
    NAMES = {
        "pitwall_lease_pod",
        "pitwall_get_lease",
        "pitwall_renew_lease",
        "pitwall_stop_lease",
    }

    def test_lease_tools_present(self) -> None:
        assert self.NAMES.issubset(TOOL_NAMES)


class TestToolRegistryCostGroup:
    NAMES = {
        "pitwall_cost_summary",
        "pitwall_recent_workloads",
    }

    def test_cost_tools_present(self) -> None:
        assert self.NAMES.issubset(TOOL_NAMES)


class TestToolRegistryAdminGroup:
    NAMES = {
        "pitwall_create_capability",
        "pitwall_update_capability",
        "pitwall_create_provider",
        "pitwall_update_provider",
        "pitwall_disable_provider",
        "pitwall_hibernate_provider",
        "pitwall_audit_log",
    }

    def test_admin_tools_present(self) -> None:
        assert self.NAMES.issubset(TOOL_NAMES)


class TestToolRegistryCopilotGroup:
    NAMES = {
        "pitwall_copilot_propose",
    }

    def test_copilot_tools_present(self) -> None:
        assert self.NAMES.issubset(TOOL_NAMES)


class TestOutOfScopeAdminVerbsBlocked:
    """Prove enable_provider and kill-switch are NOT registered MCP tools.

    These are REST-only admin verbs. enable_provider is POST /v1/admin/providers/{id}/enable.
    kill-switch is POST /v1/admin/kill-switch. Neither is wired as an MCP tool.
    """

    def test_enable_provider_not_in_registry(self) -> None:
        assert "pitwall_enable_provider" not in TOOL_NAMES

    def test_kill_switch_not_in_registry(self) -> None:
        assert "pitwall_kill_switch" not in TOOL_NAMES
        assert "pitwall_kill-switch" not in TOOL_NAMES


class TestRegisterAll:
    def test_register_all_adds_tools_to_fastmcp(self) -> None:
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("test_register")
        register_all(server)
        registered = {t.name for t in server._tool_manager.list_tools()}
        assert TOOL_NAMES.issubset(registered)

    def test_register_all_is_idempotent(self) -> None:
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("test_idempotent")
        register_all(server)
        register_all(server)
        registered = {t.name for t in server._tool_manager.list_tools()}
        assert TOOL_NAMES.issubset(registered)


class TestToolSignaturesAreWireSafe:
    def test_no_handler_uses_var_keyword_params(self) -> None:
        """FastMCP builds each tool's JSON schema from the handler signature;
        ``**kwargs`` becomes a literal *required* field named ``kwargs``, which
        makes the tool uncallable over the MCP wire protocol (regression:
        pitwall_submit_inference advertised capability params via **kwargs and
        rejected every documented call with a pydantic validation error)."""
        import inspect

        for spec in TOOL_REGISTRY:
            offenders = [
                p.name
                for p in inspect.signature(spec.handler).parameters.values()
                if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            ]
            assert not offenders, f"{spec.name} uses variadic params {offenders}"
