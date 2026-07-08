# User-Journey Catalog

Every path a user can walk through Pitwall, as a testable contract. Each hermetic journey is
automated in [`scripts/release/run-user-journeys.sh`](../../scripts/release/run-user-journeys.sh)
(the `Jnn` id below is the harness function name); live journeys require real RunPod credentials
and are covered by the [release testing checklist](release-testing-checklist.md) instead.

Hermetic journeys never use a real RunPod key and never create paid resources. They run against
the local test infrastructure (`docker-compose.testinfra.yml`) exactly the way the README tells a
new user to.

## Personas

- **Evaluator** — a stranger who cloned the repo and follows the README with no prior context.
- **Operator** — runs the services, onboards real capacity, and handles incidents.
- **Agent client** — an LLM agent (or its author) consuming the MCP server.
- **API client** — a program consuming the REST API.
- **Contributor** — runs the test suite and quality gates before sending a change.

## Hermetic journeys (automated)

| Id | Persona | Journey | Expected outcome |
| --- | --- | --- | --- |
| J01 | Evaluator | README Quick Start, verbatim: sync → testinfra → env → `db migrate` → `init --non-interactive` → `pitwall-api` → dry-run `POST /v1/inference` | Response has `"dry_run": true` and `"selected_provider_id": "prov_demo_runpod_lb"` |
| J02 | Evaluator | `init --from-seed` and fully-manual `init` flags | Both exit 0; capability + provider rows exist |
| J03 | Evaluator | `pitwall-gpu-broker seed seed/capabilities.yaml seed/providers.yaml --mark-healthy` | Exit 0; provider `healthy`; **no** warm-volume/pod path touched |
| J04 | Operator | Manual onboarding: `create-capability` → `register-endpoint` → `set-provider-health` | Each exits 0; provider resolvable and healthy |
| J05 | Operator | `pitwall-gpu-broker config check` with full env, then with `DATABASE_URL` removed | Exit 0, then fail-closed non-zero with a clear message |
| J06 | Operator | DB lifecycle: `db status` → idempotent `db migrate` → `db reset` refused without `--force` → refused for a remote host → allowed locally with `--force` → re-migrate | Guarded reset refuses exactly as documented; migrations re-apply cleanly |
| J07 | API client | Discovery: `/healthz`, `/health`, `/v1/health`, `/v1/capabilities[/name]`, `/v1/providers[/id][/health]`, `/docs`, `/openapi.json` | All 200 with expected fields |
| J08 | API client | Inference error shapes: unknown capability, malformed body | 404 and 422 with structured errors |
| J09 | Evaluator | `pitwall-gpu-broker dashboard` (TUI) boots in a pty | Alive after boot window, no traceback, all six screens registered |
| J10 | Agent client | MCP over stdio: initialize → list tools → `pitwall_list_capabilities` → `pitwall_submit_inference` (dry-run) | Server info `pitwall`; ≥ 20 tools; dry-run result returned |
| J11 | Agent client | Attempt unauthenticated MCP over SSE | Process fails closed with an explicit stdio-only message |
| J12 | Operator | Admin API: no/wrong secret rejected; with secret create capability + provider, disable/enable, `audit-capability` | 401/403 fail-closed; authorized calls succeed |
| J13 | Operator | Whole-API auth (`PITWALL_API_TOKEN`) | Data plane 401 without bearer, 200 with; health stays public |
| J14 | Operator | Inbound rate limit (`PITWALL_INBOUND_RATE_LIMIT=3/60s`) | 429 with `Retry-After` after the burst |
| J15 | API client | Budget gate: near-zero `PITWALL_MONTHLY_BUDGET_USD`, non-dry-run inference | 402 before any provider call |
| J16 | API client | OpenAI proxy safety: URL-injection path rejected; exhausted budget rejected pre-upstream | 4xx (never SSRF), 402 with zero upstream traffic |
| J17 | API client | Async job error shapes: unknown workload id on status/result/cancel | 404 structured errors |
| J18 | Operator | Webhook receiver with `PITWALL_WEBHOOK_SECRET`: unsigned, signed, and replayed deliveries | 401 unsigned; 200 signed; duplicate flagged idempotent |
| J19 | Operator | Reconciler: `check` mode with valid/invalid `REDIS_URL`; worker boot | Check exits 0/non-zero correctly; worker starts, connects DB pool, survives boot window |
| J20 | Operator | Cost exporter boots; `GET /metrics` | `pitwall_` metrics exposed on the configured port |
| J21 | Operator | Kill switch with admin secret in a dev environment (fake key, no pods) | Structured `KillReport`; API healthy afterwards |
| J22 | Operator | `warm-volume --dry-run`, then real path with fake key | Dry-run exits 0 with no spend; fake-key run fails cleanly and the admitted workload closes at `cost_actual_usd = 0` (no reservation leak) |
| J23 | Operator | Fake-key `terminate-pod`; `register-template` with missing args | Clean CLI errors, no tracebacks |
| J24 | Operator | Canonical `docker-compose.yml` validates; `.env.example` covers every required var | `docker compose config` passes; all documented-required vars present |
| J25 | Contributor | README testing commands (unit lane, security lanes) | Exact commands from the README pass |

## Live journeys (manual, real credentials required)

| Id | Persona | Journey | Covered by |
| --- | --- | --- | --- |
| L1 | Operator | Onboard a real LB endpoint end to end | [create-lb-endpoint](create-lb-endpoint.md) |
| L2 | Operator | Real vLLM endpoint + real inference round trip | [create-vllm-endpoint](create-vllm-endpoint.md) |
| L3 | Operator | Pod lease lifecycle on real GPU capacity (launch → renew → stop) | [release testing checklist](release-testing-checklist.md) |
| L4 | Operator | 16-check audit with live key | [16-check-audit-procedure](16-check-audit-procedure.md) |

## Running

```bash
docker compose -f docker-compose.testinfra.yml up -d --wait
DATABASE_URL=postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test \
REDIS_URL=redis://127.0.0.1:6380/0 \
  bash scripts/release/run-user-journeys.sh
```

The harness is destructive to the target database (it exercises the guarded `db reset`); point it
only at disposable local infrastructure. It exits non-zero if any journey fails and prints a
per-journey PASS/FAIL summary.
