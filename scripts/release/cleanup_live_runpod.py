"""Best-effort cleanup guard for the paid RunPod acceptance lane."""

from __future__ import annotations

import asyncio

from pitwall.runpod_client.pods import get_pods_by_tag_prefix, terminate_all_with_tag

_ACCEPTANCE_PREFIX = "pitwall-prov_pod-"


async def _cleanup() -> int:
    killed = await terminate_all_with_tag(_ACCEPTANCE_PREFIX)
    for _attempt in range(6):
        remaining = await get_pods_by_tag_prefix(_ACCEPTANCE_PREFIX)
        if not remaining:
            print(f"live cleanup passed: {killed} pod(s) terminated")
            return 0
        await asyncio.sleep(5)
    print(f"live cleanup failed: acceptance pods remain: {remaining}")
    return 1


def main() -> int:
    return asyncio.run(_cleanup())


if __name__ == "__main__":
    raise SystemExit(main())
