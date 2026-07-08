"""Header policy for RunPod API requests.

Applies the following policy to outgoing HTTP requests:
1. Drop hop-by-hop headers that should not be forwarded to upstream servers.
2. Drop consumer-supplied Authorization headers to prevent auth header injection.
3. Inject the RunPod Bearer token from RUNPOD_API_KEY.

See RFC 9110 §7.6.1 for the definition of hop-by-hop headers.
"""

from __future__ import annotations

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

CONSUMER_AUTH_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
    }
)


def apply_header_policy(
    incoming_headers: dict[str, str],
    runpod_api_key: str,
) -> dict[str, str]:
    """Apply header policy to produce outbound headers for a RunPod API request.

    Args:
        incoming_headers: Headers from the original consumer request.
            Keys are lowercase header names.
        runpod_api_key: The RunPod API key to use for the Bearer token.

    Returns:
        A new dict of headers to send to RunPod, with hop-by-hop and
        consumer auth headers removed, and the RunPod Bearer token injected.

    Examples:
        >>> apply_header_policy({"content-type": "application/json"}, "rp-key-123")
        {'content-type': 'application/json', 'authorization': 'Bearer rp-key-123'}

        >>> apply_header_policy(
        ...     {"authorization": "Bearer consumer-token", "connection": "keep-alive"},
        ...     "rp-key-123"
        ... )
        {'authorization': 'Bearer rp-key-123'}

        >>> apply_header_policy(
        ...     {"x-custom": "value", "proxy-authorization": "Basic abc"},
        ...     "rp-key-123"
        ... )
        {'x-custom': 'value', 'authorization': 'Bearer rp-key-123'}
    """
    outbound: dict[str, str] = {}

    for key, value in incoming_headers.items():
        key_lower = key.lower()
        if key_lower in HOP_BY_HOP_HEADERS:
            continue
        if key_lower in CONSUMER_AUTH_HEADERS:
            continue
        outbound[key] = value

    outbound["authorization"] = f"Bearer {runpod_api_key}"

    return outbound


__all__ = [
    "HOP_BY_HOP_HEADERS",
    "CONSUMER_AUTH_HEADERS",
    "apply_header_policy",
]
