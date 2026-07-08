# ADR 0001: Secure interface defaults

- Status: Accepted for the public alpha
- Date: 2026-07-17
- Decision owner: Security and API maintainers

## Context

Pitwall controls paid GPU workloads and exposes read, spend, lease, webhook, and
server-administration operations. A single optional bearer gate did not provide
least privilege, and unauthenticated network MCP or webhook listeners would
expand the trust boundary silently.

## Decision

The REST API binds to loopback by default. A non-loopback bind requires both an
API bearer credential and the administrative secret; startup otherwise exits
with `EX_CONFIG`. `PITWALL_API_TOKEN` is an all-scopes operator token. Optional
opaque tokens in `PITWALL_API_SCOPED_TOKENS` receive only explicitly listed
`read`, `spend`, `lease:mutate`, `webhook:admin`, or `server:admin` scopes.
Administrative routes additionally require `X-Pitwall-Secret`.

The inbound webhook receiver binds to loopback by default and requires HMAC
configuration for any non-loopback bind. Unauthenticated MCP is supported only
over local stdio. Network MCP without an authentication layer is rejected for
non-loopback hosts. Metrics in the canonical single-host stack are published on
loopback and are expected to sit behind operator-controlled network policy if
forwarded elsewhere.

Health and readiness endpoints disclose only component status and remain
unauthenticated. TLS is outside the Python processes and must terminate at a
trusted reverse proxy for any network deployment.

## Consequences

Loopback development remains low-friction but emits explicit insecure-mode
warnings. Production Compose requires credentials. Delegated tokens reduce the
blast radius of clients, but Pitwall remains a single-operator system without
tenant ownership or row-level authorization. Authenticated HTTP MCP is deferred
from the alpha; adding it requires a new decision and transport threat model.
