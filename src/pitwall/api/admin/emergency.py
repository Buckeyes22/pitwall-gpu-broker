"""Emergency kill-switch wrapper — persists every KillReport to pitwall.kill_log."""

from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import APIRouter
from pydantic import Field

from pitwall.api.admin.kill_switch import (
    CloudKillSwitch,
    KillReport,
    NetworkSever,
    NoOpNetworkSever,
    TailscaleNetworkSever,
)
from pitwall.core.models import PitwallModel
from pitwall.db import get_pool
from pitwall.db.kill_log import persist_kill_report

log = logging.getLogger("pitwall.api.admin.emergency")

router = APIRouter()


class KillSwitchRequest(PitwallModel):
    """Request body for POST /v1/admin/kill-switch."""

    reason: Annotated[str, Field(min_length=1, max_length=500)]
    terminate_compute: bool = Field(default=True)


def _network_sever_from_env() -> NetworkSever:
    oauth_client_id = os.environ.get("TAILSCALE_OAUTH_CLIENT_ID", "")
    oauth_client_secret = os.environ.get("TAILSCALE_OAUTH_CLIENT_SECRET", "")
    tailnet = os.environ.get("TAILSCALE_TAILNET", "")
    if oauth_client_id and oauth_client_secret and tailnet:
        return TailscaleNetworkSever(
            oauth_client_id,
            oauth_client_secret,
            tailnet,
        )
    return NoOpNetworkSever()


async def run_kill(
    reason: str,
    actor: str,
    *,
    terminate_compute: bool = True,
) -> KillReport:
    """Execute the kill switch and persist the report to pitwall.kill_log.

    Steps: deny Tailscale tag → delete tagged devices → (optionally) terminate
    all RunPod pods with the pitwall tag. Returns the KillReport, which may
    have ``errors`` populated on partial failure.

    Args:
        reason: Audit trail reason for the kill switch activation.
        actor: Identity that triggered the kill (e.g. "rest:admin", "cli:operator").
        terminate_compute: If True, terminate all tagged RunPod pods.

    Returns:
        KillReport with triggered_at, reason, and any errors encountered.
    """
    if not reason:
        raise ValueError("reason is required")
    if not actor:
        raise ValueError("actor is required")

    sever = _network_sever_from_env()
    try:
        ks = CloudKillSwitch(sever, terminate_compute=terminate_compute)
        report = await ks.activate(reason)
        pool = await get_pool()
        await persist_kill_report(
            pool,
            triggered_at=report.triggered_at,
            reason=report.reason,
            actor=actor,
            pods_terminated=report.pods_terminated,
            total_duration_ms=report.total_duration_ms,
            errors=report.errors,
        )
        log.warning(
            "kill switch activated reason=%r actor=%r errors=%s",
            reason,
            actor,
            report.errors,
        )
        return report
    finally:
        await sever.aclose()


@router.post("/v1/admin/kill-switch", response_model=KillReport)
async def activate_kill_switch(body: KillSwitchRequest) -> KillReport:
    return await run_kill(
        body.reason,
        actor="rest:admin",
        terminate_compute=body.terminate_compute,
    )


__all__ = ["KillSwitchRequest", "activate_kill_switch", "router", "run_kill"]
