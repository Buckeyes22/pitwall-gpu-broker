# Security Policy

## Supported Versions

| Version | Supported          | Notes                                    |
|---------|--------------------|------------------------------------------|
| Unreleased alpha | :white_check_mark: | Security fixes are made on the default branch |
| Published releases | :x: | No public release has been declared yet |

Pitwall is pre-1.0. The API may change in backwards-incompatible ways between minor releases. When a release reaches end-of-life, its security advisories are archived but not backported.

## Reporting a Vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Private disclosure is preferred and expected. You can report vulnerabilities through:

- **GitHub Private Vulnerability Reporting** — use the _Security_ tab on the repository, then "Report a vulnerability". This routes directly to the maintainers without exposing the details publicly.
- **Email** — no public security mailbox has been approved yet; use GitHub Private Vulnerability Reporting until one is listed here

The project targets acknowledgement within 72 hours and a substantive update
within 14 days. These are best-effort targets, not an SLA.

For non-sensitive security questions or process questions, open a regular GitHub Discussion.

## Scope

This policy covers the following Pitwall components and their security boundaries:

| Component | What is in scope |
|-----------|-----------------|
| **FastAPI control plane** (`src/pitwall/api/`) | Admin auth middleware, all `/v1/admin/*` routes including kill-switch, budget gates, and audit trails |
| **MCP server** (`src/pitwall/mcp/`) | Admin tooling and any tool that exercises privileged operations |
| **Kill-switch** (`src/pitwall/api/admin/emergency.py`, `src/pitwall/api/admin/kill_switch.py`) | `POST /v1/admin/kill-switch`; atomic termination and verification; optional network-revocation path |
| **Inbound webhook HMAC** (`src/pitwall/webhook_receiver/`, `src/pitwall/webhook_dispatcher/signer.py`) | `POST /webhooks/runpod`; constant-time signature verification with bounded replay window |
| **SSRF allow-list** (`src/pitwall/resolver/provider_urls.py`) | `runpod_endpoint_id` validation via `^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$` at `resolver/provider_urls.py::_require_endpoint_id`; outbound URL construction for serverless_lb / serverless_queue / public_endpoint providers |
| **Secrets management** | Central redaction for bearer/admin/webhook credentials, authorization headers, and credential-bearing URLs; AES-GCM storage for outbound webhook secrets |

## Known Security Model

Pitwall has two operational modes with fundamentally different trust requirements:

### Single-operator, private deployment

Pitwall has one operator, one RunPod account, and one shared budget; it is not a multi-tenant
authorization system. Bearer scopes limit what a credential can do but do not add tenant ownership
or row-level isolation.

**Production deployments must:**

1. Set `PITWALL_API_TOKEN` and `PITWALL_ADMIN_SECRET`; non-loopback API startup refuses to proceed
   without both.
2. Set `PITWALL_WEBHOOK_SECRET`; non-loopback webhook startup refuses to proceed without it.
3. Use scoped bearer tokens for delegated callers and reserve the all-scopes token for operators.
4. Bind published ports to loopback or a private interface and terminate TLS at a trusted proxy.

See the full trust model and security controls in:

- [README — Security and trust model](README.md#security-and-trust-model)
- [`docs/sdlc/14-security.md`](docs/sdlc/14-security.md)

### MCP server

The alpha MCP server is supported over local stdio only. Every network transport is rejected
because HTTP authentication is not implemented. Local process access is therefore the MCP trust
boundary.

## Security Features

| Feature | Implementation | Default |
|---------|---------------|---------|
| API authorization | Constant-time opaque bearer lookup; explicit `read`, `spend`, `lease:mutate`, `webhook:admin`, and `server:admin` scopes | Required for non-loopback API; optional with warning on loopback |
| Admin auth | `server:admin` bearer scope plus constant-time `X-Pitwall-Secret` comparison | Required for non-loopback API; admin routes fail closed if absent |
| Inbound webhook HMAC | `X-Pitwall-Webhook-Signature` verified by `webhook_dispatcher/signer.verify`; bounded timestamp window | Required for non-loopback receiver; optional on loopback |
| SSRF allow-list | `runpod_endpoint_id` validated by `^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$` at `resolver/provider_urls.py::_require_endpoint_id` | Always on |
| Kill-switch | `POST /v1/admin/kill-switch`; ordered network deny → device revoke → compute terminate; `< 30s` budget; audit-logged | Gated by admin auth |
| Fail-closed boot | Refuses to start when the runtime variables required by a service are unset | Always on |

## Out of Scope

- Multi-tenant ownership isolation is not provided; report authorization bypasses against the documented bearer scopes, but not the absence of tenant-specific row ownership.
- Third-party services that Pitwall calls (RunPod API, Tailscale, Redis, PostgreSQL). Report issues with those services to their respective vendors.
- Social-engineering attacks against operators.
