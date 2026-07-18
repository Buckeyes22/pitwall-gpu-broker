"""Best-effort cleanup guard for the paid RunPod acceptance lane."""

from __future__ import annotations

import asyncio
import os
import re

from pitwall.runpod_client.pods import get_pods_by_tag_prefix, terminate_all_with_tag

_HOSTED_RUN_ID = re.compile(r"^[0-9]+-[0-9]+$")


def _acceptance_prefix() -> str:
    run_id = os.getenv("PITWALL_LIVE_RUN_ID", "").strip()
    if not _HOSTED_RUN_ID.fullmatch(run_id):
        raise ValueError("PITWALL_LIVE_RUN_ID must identify the exact GitHub run and attempt")
    return f"pitwall-prov_pod_acceptance_{run_id}-"


async def _cleanup() -> int:
    prefix = _acceptance_prefix()
    killed = await terminate_all_with_tag(prefix)
    for _attempt in range(6):
        remaining = await get_pods_by_tag_prefix(prefix)
        if not remaining:
            print(f"live cleanup passed: {killed} pod(s) terminated")
            return 0
        await asyncio.sleep(5)
    print(f"live cleanup failed: acceptance pods remain: {remaining}")
    return 1


def main() -> int:
    try:
        return asyncio.run(_cleanup())
    except ValueError as exc:
        print(f"live cleanup refused: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
