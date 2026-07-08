"""SSRF-safe URL validation and DNS-pinned HTTPS delivery for webhooks."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
ALLOWED_WEBHOOK_PORTS = frozenset({443})


class WebhookTargetRejected(ValueError):
    """A webhook URL or one of its DNS answers violates the egress policy."""


@dataclass(frozen=True, slots=True)
class ResolvedWebhookTarget:
    """Normalized URL plus the complete public DNS answer set."""

    url: str
    hostname: str
    port: int
    request_target: str
    addresses: tuple[str, ...]


def _public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise WebhookTargetRejected("webhook DNS returned an invalid IP address") from exc
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise WebhookTargetRejected("webhook target must resolve only to global addresses")
    return address


async def _system_resolver(hostname: str, port: int) -> Sequence[str]:
    def resolve() -> list[str]:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return [str(answer[4][0]) for answer in answers]

    try:
        return await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        raise WebhookTargetRejected("webhook hostname could not be resolved") from exc


def _normalized_hostname(parsed: SplitResult, raw_url: str) -> str:
    if parsed.hostname is None:
        raise WebhookTargetRejected("webhook URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise WebhookTargetRejected("webhook URL must not contain user information")
    if "\\" in raw_url or "%" in parsed.hostname:
        raise WebhookTargetRejected("webhook URL contains an ambiguous hostname")
    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise WebhookTargetRejected("localhost webhook targets are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname.replace(".", "").isdigit():
            raise WebhookTargetRejected(
                "encoded or non-canonical numeric hosts are not allowed"
            ) from None
        try:
            return hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise WebhookTargetRejected("webhook hostname is not valid IDNA") from exc
    return address.compressed


async def resolve_webhook_target(
    url: str,
    *,
    resolver: Resolver = _system_resolver,
) -> ResolvedWebhookTarget:
    """Normalize a webhook URL and require every A/AAAA answer to be public."""

    if not isinstance(url, str) or not url or any(ord(char) < 32 for char in url):
        raise WebhookTargetRejected("webhook URL is malformed")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise WebhookTargetRejected("webhook URL must use HTTPS")
    if parsed.fragment:
        raise WebhookTargetRejected("webhook URL must not contain a fragment")
    hostname = _normalized_hostname(parsed, url)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise WebhookTargetRejected("webhook URL port is invalid") from exc
    if port not in ALLOWED_WEBHOOK_PORTS:
        raise WebhookTargetRejected("webhook URL port is not allowed")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        raw_addresses = await resolver(hostname, port)
    else:
        raw_addresses = [literal.compressed]
    if not raw_addresses:
        raise WebhookTargetRejected("webhook hostname returned no addresses")
    addresses = tuple(dict.fromkeys(_public_ip(value).compressed for value in raw_addresses))

    display_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = display_host if port == 443 else f"{display_host}:{port}"
    path = parsed.path or "/"
    normalized_url = urlunsplit(("https", netloc, path, parsed.query, ""))
    request_target = path + (f"?{parsed.query}" if parsed.query else "")
    return ResolvedWebhookTarget(
        url=normalized_url,
        hostname=hostname,
        port=port,
        request_target=request_target,
        addresses=addresses,
    )


def redact_webhook_url(url: str) -> str:
    """Return destination metadata without query parameters or fragments."""

    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that never performs a second hostname DNS lookup."""

    def __init__(self, target: ResolvedWebhookTarget, address: str, timeout: float) -> None:
        ssl_context = ssl.create_default_context()
        super().__init__(
            target.hostname,
            target.port,
            timeout=timeout,
            context=ssl_context,
        )
        self._pinned_address = address
        self._ssl_context = ssl_context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            None,
        )
        try:
            peer = _public_ip(str(raw_socket.getpeername()[0]))
            if peer != ipaddress.ip_address(self._pinned_address):
                raise WebhookTargetRejected("connected webhook peer did not match pinned DNS")
            self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


def _post_pinned_sync(
    target: ResolvedWebhookTarget,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: float,
) -> int:
    connection = _PinnedHTTPSConnection(target, target.addresses[0], timeout_seconds)
    try:
        connection.connect()
        connection.request("POST", target.request_target, body=body, headers=headers)
        response = connection.getresponse()
        response.read(4096)
        return response.status
    finally:
        connection.close()


async def post_pinned_https(
    target: ResolvedWebhookTarget,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: float,
) -> int:
    """POST to one validated address with TLS SNI and peer-address verification."""

    return await asyncio.to_thread(
        _post_pinned_sync,
        target,
        body,
        headers,
        timeout_seconds,
    )


__all__ = [
    "ALLOWED_WEBHOOK_PORTS",
    "ResolvedWebhookTarget",
    "Resolver",
    "WebhookTargetRejected",
    "post_pinned_https",
    "redact_webhook_url",
    "resolve_webhook_target",
]
