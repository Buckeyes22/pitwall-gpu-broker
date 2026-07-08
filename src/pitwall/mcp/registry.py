"""MCP tool registry — stable names, descriptions, schemas, and handler callables.

This module defines the canonical ``TOOL_REGISTRY`` containing all 23
``pitwall_*`` MCP tools.  Each entry is a ``ToolSpec`` dataclass with a
stable tool name, human-readable description, and a handler callable whose
typed parameters become the tool's input schema.

Every registry entry points at its production tool handler; this module contains
metadata and registration only, not a parallel mock implementation.

Usage::

    from pitwall.mcp.registry import register_all
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pitwall")
    register_all(mcp)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pitwall.mcp.tools.admin import (
    pitwall_create_capability,
    pitwall_create_provider,
    pitwall_disable_provider,
    pitwall_hibernate_provider,
    pitwall_update_capability,
    pitwall_update_provider,
)
from pitwall.mcp.tools.audit import pitwall_audit_log
from pitwall.mcp.tools.copilot import pitwall_copilot_propose
from pitwall.mcp.tools.cost import (
    pitwall_cost_summary,
    pitwall_recent_workloads,
)
from pitwall.mcp.tools.discovery import (
    pitwall_describe_capability,
    pitwall_get_provider_health,
    pitwall_list_capabilities,
    pitwall_list_providers,
)
from pitwall.mcp.tools.inference import (
    pitwall_cancel_job,
    pitwall_get_job_result,
    pitwall_get_job_status,
    pitwall_submit_inference,
    pitwall_submit_job,
)
from pitwall.mcp.tools.leases import (
    pitwall_get_lease,
    pitwall_lease_pod,
    pitwall_renew_lease,
    pitwall_stop_lease,
)

TOOL_NAMES: frozenset[str] = frozenset(
    {
        "pitwall_list_capabilities",
        "pitwall_describe_capability",
        "pitwall_list_providers",
        "pitwall_get_provider_health",
        "pitwall_submit_inference",
        "pitwall_submit_job",
        "pitwall_get_job_status",
        "pitwall_get_job_result",
        "pitwall_cancel_job",
        "pitwall_lease_pod",
        "pitwall_get_lease",
        "pitwall_renew_lease",
        "pitwall_stop_lease",
        "pitwall_cost_summary",
        "pitwall_recent_workloads",
        "pitwall_create_capability",
        "pitwall_update_capability",
        "pitwall_create_provider",
        "pitwall_update_provider",
        "pitwall_disable_provider",
        "pitwall_hibernate_provider",
        "pitwall_audit_log",
        "pitwall_copilot_propose",
    }
)

assert len(TOOL_NAMES) == 23


@dataclass(frozen=True)
class ToolSpec:
    """A single registered MCP tool."""

    name: str
    description: str
    handler: Callable[..., dict[str, Any]] | Callable[..., Awaitable[dict[str, Any]]]


TOOL_REGISTRY: list[ToolSpec] = [
    ToolSpec(
        name="pitwall_list_capabilities",
        description="List all registered capabilities, optionally filtered by class, cost mode, or enabled state.",
        handler=pitwall_list_capabilities,
    ),
    ToolSpec(
        name="pitwall_describe_capability",
        description="Return full details for a single capability by name or ID.",
        handler=pitwall_describe_capability,
    ),
    ToolSpec(
        name="pitwall_list_providers",
        description="List all registered providers, optionally filtered by capability, type, or enabled state.",
        handler=pitwall_list_providers,
    ),
    ToolSpec(
        name="pitwall_get_provider_health",
        description="Return health status, cooldown state, and recent error rate for a single provider.",
        handler=pitwall_get_provider_health,
    ),
    ToolSpec(
        name="pitwall_submit_inference",
        description="Submit a synchronous inference request to a capability. Returns the result directly.",
        handler=pitwall_submit_inference,
    ),
    ToolSpec(
        name="pitwall_submit_job",
        description="Submit an asynchronous job to a capability. Returns a workload ID for polling.",
        handler=pitwall_submit_job,
    ),
    ToolSpec(
        name="pitwall_get_job_status",
        description="Return the current state of an async job by workload ID.",
        handler=pitwall_get_job_status,
    ),
    ToolSpec(
        name="pitwall_get_job_result",
        description="Return the completed result of an async job by workload ID.",
        handler=pitwall_get_job_result,
    ),
    ToolSpec(
        name="pitwall_cancel_job",
        description="Cancel a pending or running async job by workload ID.",
        handler=pitwall_cancel_job,
    ),
    ToolSpec(
        name="pitwall_lease_pod",
        description="Create a pod lease for a capability. Routes to a pod_lease provider and tracks readiness.",
        handler=pitwall_lease_pod,
    ),
    ToolSpec(
        name="pitwall_get_lease",
        description="Return the current state and details of a pod lease.",
        handler=pitwall_get_lease,
    ),
    ToolSpec(
        name="pitwall_renew_lease",
        description="Extend an active pod lease by a number of minutes.",
        handler=pitwall_renew_lease,
    ),
    ToolSpec(
        name="pitwall_stop_lease",
        description="Stop and tear down an active pod lease.",
        handler=pitwall_stop_lease,
    ),
    ToolSpec(
        name="pitwall_cost_summary",
        description="Return aggregated cost summary, optionally filtered by capability class and date range.",
        handler=pitwall_cost_summary,
    ),
    ToolSpec(
        name="pitwall_recent_workloads",
        description="Return a list of recent workloads with their states and cost estimates.",
        handler=pitwall_recent_workloads,
    ),
    ToolSpec(
        name="pitwall_create_capability",
        description="Register a new capability with the Pitwall broker. Admin-only.",
        handler=pitwall_create_capability,
    ),
    ToolSpec(
        name="pitwall_update_capability",
        description="Update fields on an existing capability. Admin-only.",
        handler=pitwall_update_capability,
    ),
    ToolSpec(
        name="pitwall_create_provider",
        description="Register a new provider (RunPod endpoint) for a capability. Admin-only.",
        handler=pitwall_create_provider,
    ),
    ToolSpec(
        name="pitwall_update_provider",
        description="Update fields on an existing provider. Admin-only.",
        handler=pitwall_update_provider,
    ),
    ToolSpec(
        name="pitwall_disable_provider",
        description="Disable a provider so it is excluded from routing. Admin-only.",
        handler=pitwall_disable_provider,
    ),
    ToolSpec(
        name="pitwall_hibernate_provider",
        description="Hibernate a serverless provider by scaling workers to zero. Admin-only.",
        handler=pitwall_hibernate_provider,
    ),
    ToolSpec(
        name="pitwall_audit_log",
        description="Return config mutation audit entries, optionally filtered by entity and action.",
        handler=pitwall_audit_log,
    ),
    ToolSpec(
        name="pitwall_copilot_propose",
        description=(
            "Return a proposal-only GitOps plan/diff for an operator intent. Never applies changes."
        ),
        handler=pitwall_copilot_propose,
    ),
]

_REGISTRY_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_REGISTRY}

assert len(_REGISTRY_BY_NAME) == 23
assert set(_REGISTRY_BY_NAME) == TOOL_NAMES


def register_all(server: Any) -> None:
    """Register every tool in ``TOOL_REGISTRY`` with a FastMCP server."""
    for spec in TOOL_REGISTRY:
        server.tool(name=spec.name, description=spec.description)(spec.handler)


__all__ = [
    "TOOL_NAMES",
    "TOOL_REGISTRY",
    "ToolSpec",
    "register_all",
]
