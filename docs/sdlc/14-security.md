# Security Model

Pitwall handles a credentialed outbound surface (RunPod API keys, paid GPU calls) and an
administrative control plane (audit, kill-switch). Its security posture is fail-closed boot,
defense-in-depth on the admin + webhook surfaces, and a static-analysis gate chain in CI. The
runtime controls below were authored find→fix (a test surfaced the weakness, then a paired fix
closed it) under the release program security track.

## 1. Trust boundaries

| Boundary | Threats | Control |
|----------|---------|---------|
| Inbound API (non-health routes) | unauthorized public API use | opt-in bearer gate |
| Inbound admin (`/v1/admin/*`) | unauthorized audit / kill-switch | fail-closed, constant-time shared-secret gate |
| Inbound API edge | brute-force / DoS / abusive callers | opt-in token-bucket limiter |
| Inbound webhook (`/webhooks/runpod`) | forged terminal-status deliveries | opt-in HMAC verification |
| Bare HTTP entrypoints | accidental network exposure | loopback bind by default |
| Outbound RunPod URL construction | SSRF via `runpod_endpoint_id` | strict allow-list at one chokepoint |
| Outbound webhook delivery | tampering / replay of our callbacks | timestamped HMAC signing |
| Error / health responses | secret disclosure | non-disclosure assertions |
| Dependencies & source | CVEs, hardcoded secrets, unsafe patterns | pip-audit / detect-secrets / bandit |

## 2. Fail-closed boot

The API calls `require_runtime_env("api")` at import and then reads `RUNPOD_API_KEY`,
`DATABASE_URL`, and `REDIS_URL` from `os.environ` (`src/pitwall/api/app.py:41`,
`src/pitwall/api/app.py:43`, `src/pitwall/api/app.py:44`, `src/pitwall/api/app.py:45`).
The shared config map uses those three vars for `api` / `reconciler` / `worker` / `mcp`,
uses only `DATABASE_URL` for `cost-exporter`, and has no import-time required vars for
`webhook` (`src/pitwall/config.py:847`, `src/pitwall/config.py:850`,
`src/pitwall/config.py:851`, `src/pitwall/config.py:852`, `src/pitwall/config.py:853`,
`src/pitwall/config.py:854`, `src/pitwall/config.py:855`). `require_runtime_env(...)`
prints names only and exits `os.EX_CONFIG` on config errors (`src/pitwall/config.py:937`,
`src/pitwall/config.py:940`, `src/pitwall/config.py:941`, `src/pitwall/config.py:942`,
`src/pitwall/config.py:944`, `src/pitwall/config.py:947`, `src/pitwall/config.py:948`,
`src/pitwall/config.py:952`, `src/pitwall/config.py:955`). This boot gate is separate from
the fail-closed admin gate below.

## 3. Fail-closed admin

`AdminSecretMiddleware` is always installed on the FastAPI app, regardless of whether
`PITWALL_ADMIN_SECRET` is configured (`src/pitwall/api/app.py:47`,
`src/pitwall/api/app.py:290`). It gates `/v1/admin` and `/v1/admin/*`; if the secret is
unset, those paths return 401 before handlers run, with detail:
`admin routes disabled: PITWALL_ADMIN_SECRET is not configured`
(`src/pitwall/api/app.py:159`, `src/pitwall/api/app.py:160`, `src/pitwall/api/app.py:162`,
`src/pitwall/api/app.py:163`, `src/pitwall/api/app.py:164`, `src/pitwall/api/app.py:167`).
This is fail-closed admin auth, not fail-closed process boot.

## 4. Whole-API bearer token

`PITWALL_API_TOKEN` is an all-scopes operator credential and is required for a
non-loopback API bind. When set,
`ApiTokenMiddleware` checks `Authorization: Bearer <token>` on every non-public-health path
and returns 401 `{"detail": "invalid or missing bearer token"}` plus
`WWW-Authenticate: Bearer` for a missing or bad token (`src/pitwall/api/app.py:187`,
`src/pitwall/api/app.py:193`, `src/pitwall/api/app.py:195`, `src/pitwall/api/app.py:196`,
`src/pitwall/api/app.py:197`, `src/pitwall/api/app.py:198`, `src/pitwall/api/app.py:200`,
`src/pitwall/api/app.py:201`). Surface mechanics stay in `02-api-rest.md`.

## 5. Inbound rate limiting

`PITWALL_INBOUND_RATE_LIMIT` is an opt-in REST-edge abuse control. Unset or blank means no
config; invalid values are fatal configuration errors rather than a reason to
disable the limiter.
The middleware is installed on the app and passes through when config is absent
(`src/pitwall/api/app.py:221`, `src/pitwall/api/app.py:222`, `src/pitwall/api/app.py:223`,
`src/pitwall/api/app.py:292`). When exhausted, it returns 429
`{"detail": "rate limit exceeded"}` with `Retry-After` (`src/pitwall/api/app.py:238`,
`src/pitwall/api/app.py:240`, `src/pitwall/api/app.py:241`, `src/pitwall/api/app.py:242`).
Token-bucket details stay in `11-rate-limiting.md`.

## 6. Private-by-default network binding

The three HTTP console scripts route to separate entrypoint modules (`pyproject.toml:48`,
`pyproject.toml:51`, `pyproject.toml:52`). Their `main()` functions default the uvicorn host
to `127.0.0.1` and require an explicit host env var to bind elsewhere:
`PITWALL_API_HOST`, `PITWALL_WEBHOOK_HOST`, and `PITWALL_COST_EXPORTER_HOST`
(`src/pitwall/api/__main__.py:11`, `src/pitwall/api/__main__.py:15`,
`src/pitwall/webhook_receiver/__main__.py:11`, `src/pitwall/webhook_receiver/__main__.py:15`,
`src/pitwall/cost_exporter/__main__.py:11`, `src/pitwall/cost_exporter/__main__.py:15`).
This reduces default network exposure for bare `pitwall-api`, `pitwall-webhook`, and
`pitwall-cost-exporter` runs.

## 7. Admin authentication — constant time

`AdminSecretMiddleware` (`src/pitwall/api/app.py`) gates every request whose path is `/v1/admin` or
starts with `/v1/admin/`. The supplied `X-Pitwall-Secret` header is compared to the configured
secret with `hmac.compare_digest`, **not** `!=` — so the gate leaks neither the secret's length nor
its prefix through response timing (`src/pitwall/api/app.py:176`, `src/pitwall/api/app.py:177`).
Missing admin-secret behavior is covered by §3.

- Tests: `tests/security/test_admin_constant_time.py` pins the constant-time source (a find→fix
  static guard) + a through-the-stack table; `tests/security/test_admin_auth_surface.py` enumerates
  every `/v1/admin/*` route from `app.routes` (so future admin routes are auto-covered) and asserts
  401 without/with-wrong secret, non-401 with the right one.
- Note: auth is app-level middleware, not per-route — the bare-router audit tests (`tests/audit/`)
  therefore make no 401 assertion.

## 8. SSRF — provider-URL endpoint-id allow-list

`runpod_endpoint_id` is interpolated into outbound RunPod URLs as the **host label** for
`serverless_lb` (`https://{id}.api.runpod.ai`) and into the **path** for `serverless_queue`
(`https://api.runpod.ai/v2/{id}`). Unvalidated, a hostile id could redirect Pitwall's own
credentialed calls (host-label injection, path traversal, userinfo/port injection, or the cloud
metadata IP `169.254.169.254`).

Control: an allow-list regex `^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$` is enforced at the single chokepoint
`resolver/provider_urls.py::_require_endpoint_id`, through which **every** URL builder funnels. The
charset bans `. / : @` and whitespace by construction. Prefer extending the charset over relaxing
it; never interpolate an unvalidated id into an outbound URL.

- Tests: `tests/security/test_provider_url_ssrf.py` drives hostile ids through `lb_url`/`queue_url`
  (raise `ValueError`) plus an allow-list host invariant (`*.api.runpod.ai` only) that stays green
  before and after the fix. See `04-routing.md`.

## 9. Inbound webhook authentication — opt-in HMAC

When `PITWALL_WEBHOOK_SECRET` is set, the RunPod receiver requires every inbound delivery to carry a
valid `X-Pitwall-Webhook-Signature`, verified through the **outbound dispatcher's** signer
(`webhook_dispatcher.signer.verify` — constant-time `compare_digest` + a 300s replay window).
Reusing the proven verifier avoids a second HMAC implementation. Unset secret = unchanged ingress
(opt-in). Invalid/missing signature → 401 `{"ok": false, "detail": ...}`.

- Tests: `tests/security/test_webhook_receiver_unauthenticated.py` pins the insecure default (unset
  secret → 200, stays green post-fix); `tests/security/test_webhook_receiver_signed.py` drives
  accept / missing / wrong-secret / tampered-body / replayed-stale-timestamp plus a static guard
  that the route delegates to the shared verifier. See `09-webhooks.md`.

## 10. Outbound webhook signing

Outbound deliveries are signed with a timestamped HMAC-SHA256 scheme
(`webhook_dispatcher/signer.py`): the signed message is `{timestamp}.{body}`, the header is
`t={ts},v1={hexdigest}`, and `verify` enforces a 300s `max_age` (replay window) with a constant-time
`compare_digest`. See `09-webhooks.md`.

## 11. Secret non-disclosure

Secret *values* must never appear in env-validation errors, the `/health` surface, or error
envelopes. `tests/security/test_*` assert that boot-failure messages name the missing
variable but never echo a value, and that health/error responses carry no secret material.

## 12. API fuzzing

`tests/security/test_schemathesis_fuzz.py` (Schemathesis 4.x) drives all 43 current OpenAPI
operations with schema-derived and malformed input and asserts `not_a_server_error`. Protected,
health, and webhook-management routes are included; none are silently excluded. See
`17-testing-strategy.md`.

## 13. Static-analysis gate chain (CI)

| Gate | Tool | Policy | Make target |
|------|------|--------|-------------|
| SAST | bandit | MEDIUM/MEDIUM (`-ll -ii`); accepted findings in `tools/security/bandit-baseline.json` (triaged, one bullet each in `tools/security/README.md`) | `make sec` |
| Dependency CVEs | pip-audit | audits the locked env; ignores require a dated justification in `tools/security/pip-audit-ignore.txt` (bump-by-default) | `make sec` |
| Secrets | detect-secrets | scans against `.secrets.baseline`; baseline drift fails the hook | (pre-commit + CI) |

**No inline suppressions:** `# nosec` (bandit), `# noqa` (ruff), `# type: ignore` (mypy) are banned
by `tools/guards/python_policy.py` unless they carry both a rule code and a `# reason:`; `except
Exception` needs a trailing `# reason:`. Accepted SAST findings live in the baseline, not inline.

## 14. Operational security

The kill-switch (`15-operations.md`) severs Tailscale ACL → tagged devices → RunPod compute in
<30s and persists every activation to `pitwall.kill_log`. It is reachable only through the
constant-time-gated `/v1/admin/kill-switch` route (and the CLI).

## 15. Configuration summary

| Env var | Effect |
|---------|--------|
| `RUNPOD_API_KEY` / `DATABASE_URL` / `REDIS_URL` | required for the API core boot path (`src/pitwall/api/app.py:41`, `src/pitwall/api/app.py:43`, `src/pitwall/api/app.py:44`, `src/pitwall/api/app.py:45`) |
| `PITWALL_ADMIN_SECRET` | configures admin auth; unset makes `/v1/admin/*` return 401 (`src/pitwall/api/app.py:47`, `src/pitwall/api/app.py:162`, `src/pitwall/api/app.py:167`, `src/pitwall/api/app.py:290`) |
| `PITWALL_API_TOKEN` | whole-API bearer gate; required for non-loopback API binding and optional with a warning on loopback |
| `PITWALL_INBOUND_RATE_LIMIT` | defaults to `120/60s`; exhausted buckets return 429 + `Retry-After`; invalid configuration fails startup |
| `PITWALL_API_MAX_BODY_BYTES` | defaults to 8 MiB and bounds fixed-length and chunked request bodies before route execution |
| `PITWALL_WEBHOOK_SECRET` | when set, requires HMAC on inbound `/webhooks/runpod` (`src/pitwall/webhook_receiver/__init__.py:37`, `src/pitwall/webhook_receiver/__init__.py:93`, `src/pitwall/webhook_receiver/__init__.py:95`, `src/pitwall/webhook_receiver/__init__.py:96`, `src/pitwall/webhook_receiver/__init__.py:98`) |
| `PITWALL_API_HOST` / `PITWALL_WEBHOOK_HOST` / `PITWALL_COST_EXPORTER_HOST` | override loopback bind defaults for `pitwall-api`, `pitwall-webhook`, and `pitwall-cost-exporter` (`src/pitwall/api/__main__.py:11`, `src/pitwall/webhook_receiver/__main__.py:11`, `src/pitwall/cost_exporter/__main__.py:11`) |

See `tools/security/README.md` for the triage playbook and `17-testing-strategy.md` §4 for the full
security test inventory.
