# Security scan triage

## bandit
- Config: `[tool.bandit]` in `pyproject.toml`. Severity/confidence gate: MEDIUM/MEDIUM (`-ll -ii`).
- Suppression: **NEVER use inline `# nosec`** (banned by repo policy, same family as `# noqa`).
  Accepted findings live in `tools/security/bandit-baseline.json`, regenerated via `make sec-baseline`.
- Before adding a finding to the baseline, write one bullet here: file:line, rule id, why it is safe.

### Accepted (baseline) findings
- `src/pitwall/cost/exporter.py:259` B104(bind all interfaces): intentional metrics-service
  runner used by a container entry point; canonical host publication is loopback-only and the
  deployment is responsible for any broader network boundary.

## pip-audit
- Audits the locked env: `pip-audit -r <(uv export --format requirements-txt --no-hashes)`.
- Triage: a CVE may be ignored ONLY if (a) no fix version exists AND (b) the vulnerable
  code path is unreachable from pitwall, OR (c) a fix bump is scheduled. Add the advisory
  ID + dated justification to `tools/security/pip-audit-ignore.txt`. Default action is to
  bump the dependency, not ignore.

## Semgrep
- `make sec-semgrep` scans production Python with the Semgrep Registry's Python and
  security-audit rules. It is intentionally separate from `make sec` because the first
  run downloads rules and Semgrep is a machine-local tool rather than a locked project
  dependency.
- Metrics are disabled. Paths in `.semgrepignore` exclude local environments, generated
  intelligence indexes, caches, and existing machine-generated security artifacts.
- `tools/security/semgrep.yml` restores MD5 detection after excluding a noisy registry rule;
  the local replacement reports any MD5 use in production Python.
- Triage findings under the same policy as Bandit: fix the code or document an accepted
  finding in a baseline/configuration file; do not add inline suppression comments.

## detect-secrets
- Baseline: `.secrets.baseline`. Pre-commit + CI both invoke the canonical
  `uv run python tools/security/check_secrets.py` gate over every tracked file,
  including tests. It compares stable type/hash/file fingerprints, so timestamps
  and moved line numbers cannot hide or invent drift.
- A new high-entropy string, any unreviewed entry, or a stale baseline item fails.
  Resolve real secrets by removing and rotating them. For fixtures, regenerate
  with the same exclusions and mark each reviewed item `is_secret: false`.
- Composes with tools/guards/repo_text_policy.py (literal banned strings L11/L12) -
  detect-secrets catches entropy, repo_text_policy catches known literals. Keep both.

## Security test suite

Runtime security tests live in `tests/security/` (markers `security` and `fuzz`). One
command each, mirrored by the `security-*` CI jobs:

- `make sec-test` → `pytest -m "security and not fuzz" tests/security` (single `-m`
  expression — pytest keeps only the *last* `-m`, so `-m security -m "not fuzz"` would
  silently drop `security`).
- `make sec-fuzz` → `pytest -m fuzz tests/security` (the schemathesis run; CI job
  `security-fuzz`).

Each product hardening was authored find→fix (a test surfaces the weakness, then the
paired one-line product change closes it):

- **Admin auth (constant time).** `AdminSecretMiddleware` compares the
  `X-Pitwall-Secret` header with `hmac.compare_digest`, not `!=`, so the gate leaks
  neither the secret's length nor its prefix through response timing.
  `test_admin_constant_time.py` pins the constant-time source + a through-the-stack
  table; `test_admin_auth_surface.py` enumerates every `/v1/admin/*` route from
  `app.routes` (auto-covering future routes) and asserts 401 without/with-wrong secret.

- **Inbound webhook HMAC (required in the canonical deployment).** When
  `PITWALL_WEBHOOK_SECRET` is set, the
  RunPod receiver verifies `X-Pitwall-Webhook-Signature` through the outbound
  dispatcher's signer (`webhook_dispatcher.signer.verify` — constant-time compare +
  300s replay window). The canonical Compose stack requires the secret; an unset value is
  supported only for explicitly isolated development. Signed accept/reject/tamper/replay cases
  live in `test_webhook_receiver_signed.py`.

- **Schemathesis fuzz.** `test_schemathesis_fuzz.py` drives all 43 public
  operations, including admin, health, and webhook-management routes, with
  generated input and asserts no 5xx (`not_a_server_error`). No route is silently
  excluded. Common 4xx/503 envelopes and auth/throttle headers are declared in
  the OpenAPI document; compatibility is checked separately.

### SSRF — provider-URL endpoint-id validation
- `runpod_endpoint_id` is interpolated into outbound RunPod URLs: as the **host label**
  for `serverless_lb` (`https://{id}.api.runpod.ai`) and into the **path** for
  `serverless_queue` (`https://api.runpod.ai/v2/{id}`). Unvalidated, a hostile id
  redirects Pitwall's own credentialed outbound calls (host-label injection, path
  traversal, userinfo/port injection, or the cloud metadata IP `169.254.169.254`).
- Fix: an allow-list regex `^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$` enforced at the single
  chokepoint `resolver/provider_urls.py::_require_endpoint_id`, through which every URL
  builder funnels. The charset bans `. / : @` and whitespace by construction.
- `test_provider_url_ssrf.py` drives hostile ids through `lb_url`/`queue_url` (raise
  `ValueError`) plus an allow-list host invariant (`*.api.runpod.ai` only) that stays
  green before and after the fix. Prefer extending the allow-list charset over relaxing
  it; never interpolate an un-validated id into an outbound URL.
