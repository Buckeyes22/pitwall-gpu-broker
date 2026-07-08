"""Emergency teardown of cloud resources. Target runtime <30s.

Steps: (1) deny Tailscale tag, (2) delete all tagged devices, (3) terminate
any live RunPod pods in the account.

R2 key rotation is not a kill-switch step. Pitwall vends scoped temporary R2
credentials to pods, so parent R2 keys stay in the control plane and incident
response can rely on credential expiry instead of rotating pod-held keys.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel

from pitwall.runpod_client.pods import get_pods_by_tag_prefix, terminate_all_with_tag
from pitwall.staging_store import StagingStore, get_staging_store

log = logging.getLogger("pitwall.api.admin.kill_switch")

DEFAULT_TAG = "tag:pitwall-cloud-worker"


class KillReport(BaseModel):
    triggered_at: datetime
    reason: str
    tailscale_acl_updated: bool
    devices_removed: int
    pods_terminated: int
    total_duration_ms: int
    errors: list[str]


class NetworkSever(Protocol):
    async def deny_all(self, tag: str = DEFAULT_TAG) -> bool:
        """Deny network access for workloads matching *tag*.

        Returns True when a real network deny operation was applied.
        """
        ...

    async def revoke_devices(self, tag: str = DEFAULT_TAG) -> int:
        """Revoke network devices matching *tag* and return the count."""
        ...

    async def aclose(self) -> None:
        """Release any held resources."""
        ...


class NoOpNetworkSever:
    async def deny_all(self, tag: str = DEFAULT_TAG) -> bool:
        return False

    async def revoke_devices(self, tag: str = DEFAULT_TAG) -> int:
        return 0

    async def aclose(self) -> None:
        pass


_DEFAULT_NETWORK_SEVER = NoOpNetworkSever()


class TailscaleNetworkSever:
    def __init__(
        self,
        oauth_client_id: str,
        oauth_client_secret: str,
        tailnet: str,
    ) -> None:
        if not (oauth_client_id and oauth_client_secret and tailnet):
            raise ValueError("oauth_client_id, oauth_client_secret, tailnet are required")
        self._client_id = oauth_client_id
        self._client_secret = oauth_client_secret
        self._tailnet = tailnet
        self._http = None

    async def deny_all(self, tag: str = DEFAULT_TAG) -> bool:
        import httpx

        token = await self._access_token()
        async with httpx.AsyncClient(timeout=30.0) as client:
            get = await client.get(
                f"https://api.tailscale.com/api/v2/tailnet/{self._tailnet}/acl",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            get.raise_for_status()
            etag = get.headers.get("ETag", "")
            acl = get.json()
            acl["acls"] = [r for r in acl.get("acls", []) if tag not in r.get("src", [])]
            acl.setdefault("acls", []).append(
                {
                    "action": "accept",
                    "src": [tag],
                    "dst": [],
                }
            )
            post = await client.post(
                f"https://api.tailscale.com/api/v2/tailnet/{self._tailnet}/acl",
                headers={
                    "Authorization": f"Bearer {token}",
                    "If-Match": etag,
                    "Content-Type": "application/json",
                },
                json=acl,
            )
            post.raise_for_status()
            return True

    async def revoke_devices(self, tag: str = DEFAULT_TAG) -> int:
        import httpx

        token = await self._access_token()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"https://api.tailscale.com/api/v2/tailnet/{self._tailnet}/devices",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            devices = [d for d in resp.json().get("devices", []) if tag in (d.get("tags") or [])]
            for d in devices:
                del_resp = await client.delete(
                    f"https://api.tailscale.com/api/v2/device/{d['id']}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                del_resp.raise_for_status()
            return len(devices)

    async def _access_token(self) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.tailscale.com/api/v2/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
            resp.raise_for_status()
            body = resp.json()
            return cast(str, body["access_token"])

    async def aclose(self) -> None:
        pass

    async def set_tag_deny_all(self, tag: str = DEFAULT_TAG) -> None:
        await self.deny_all(tag)

    async def revoke_all(self, tag: str = DEFAULT_TAG) -> int:
        return await self.revoke_devices(tag)


class TailscaleProvisioner(TailscaleNetworkSever):
    """Backward-compatible name for the Tailscale network sever."""


class CloudKillSwitch:
    def __init__(
        self,
        sever: NetworkSever = _DEFAULT_NETWORK_SEVER,
        *,
        terminate_compute: bool = True,
        tag: str = DEFAULT_TAG,
        staging_store: StagingStore | None = None,
    ) -> None:
        self.sever = sever
        self.terminate_compute = terminate_compute
        self.tag = tag
        self.staging_store = staging_store

    async def _cleanup_r2_staging(self, pods: list[dict[str, Any]]) -> list[str]:
        """Delete R2 staging prefixes for terminated pods.

        Returns a list of error messages (empty if no errors).
        """
        errors: list[str] = []
        try:
            results = (self.staging_store or get_staging_store()).cleanup_pod_artifacts(pods)
            for result in results:
                if result.errors:
                    for err in result.errors:
                        errors.append(f"pod {result.pod_id}: {err}")
                if result.objects_deleted > 0:
                    log.info(
                        "R2 cleanup: deleted %d objects for pod %s (%s)",
                        result.objects_deleted,
                        result.pod_id,
                        result.pod_name,
                    )
        except Exception as exc:  # pragma: no cover  # reason: emergency cleanup is best-effort; R2 failures are recorded and the kill path continues.
            errors.append(f"R2 cleanup failed: {exc}")
            log.exception("R2 cleanup failed: %s", exc)

        return errors

    async def activate(self, reason: str) -> KillReport:
        if not reason:
            raise ValueError("reason is required (audit trail)")

        t0 = time.perf_counter()
        errors: list[str] = []
        network_severed = False
        devices = 0
        compute_n = 0

        try:
            network_severed = await self.sever.deny_all(self.tag)
        except Exception as exc:  # pragma: no cover  # reason: emergency path records partial failure and continues.
            errors.append(f"acl: {exc}")

        try:
            devices = await self.sever.revoke_devices(self.tag)
        except Exception as exc:  # pragma: no cover  # reason: emergency path records partial failure and continues.
            errors.append(f"devices: {exc}")

        if self.terminate_compute:
            pods_to_terminate: list[dict[str, Any]] = []
            try:
                compute_n = await terminate_all_with_tag(name_prefix="pitwall-")
            except Exception as exc:  # pragma: no cover  # reason: emergency path records partial failure and continues.
                errors.append(f"compute: {exc}")

            if compute_n > 0:
                # pragma: no cover  # reason: best-effort pod enumeration for R2
                # cleanup; on failure pods_to_terminate stays [] and is skipped.
                with contextlib.suppress(Exception):
                    pods_to_terminate = await get_pods_by_tag_prefix(name_prefix="pitwall-")

            if pods_to_terminate:
                cleanup_errors = await self._cleanup_r2_staging(pods_to_terminate)
                for err in cleanup_errors:
                    errors.append(f"r2_cleanup: {err}")

        duration_ms = int((time.perf_counter() - t0) * 1000)
        return KillReport(
            triggered_at=datetime.now(UTC),
            reason=reason,
            tailscale_acl_updated=network_severed,
            devices_removed=devices,
            pods_terminated=compute_n,
            total_duration_ms=duration_ms,
            errors=errors,
        )


__all__ = [
    "CloudKillSwitch",
    "KillReport",
    "DEFAULT_TAG",
    "NetworkSever",
    "NoOpNetworkSever",
    "TailscaleNetworkSever",
    "TailscaleProvisioner",
]
