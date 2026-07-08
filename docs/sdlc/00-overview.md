# Pitwall — System Overview

Pitwall is a **RunPod GPU broker**: a control plane that accepts capability requests (embeddings,
LLM inference, pod leases), routes each to the best-fit RunPod provider, estimates and gates its
cost against a budget before any spend, executes the call, and reconciles the result — with a
hard pre-spend audit, kill-switch, and full cost/observability surface around it.

## What problem it solves

GPU capacity on RunPod comes in several shapes (serverless queue, serverless load-balanced,
public OpenAI-compatible endpoints, and raw pod leases), each with different URLs, billing models,
cold-start behavior, and failure modes. Pitwall presents a **single capability-oriented API** over
all of them and enforces the operational guardrails a team needs to run paid GPU workloads safely:
a monthly budget + per-request cap, a 16-point readiness audit before spending, idempotent
webhook ingestion, health/cooldown-aware routing with fallback chains, and an emergency
kill-switch that severs network + compute in under 30 seconds.

## Core capabilities

- **Capability registry & routing** — capabilities and providers are stored in Postgres; the
  planner selects a provider via hard-constraint filtering, health/cooldown gating, scoring, and
  explicit fallback chains (`routing/`, `resolver/`). See `04-routing.md`.
- **Cost estimation & budget admission** — every workload is priced (per-second / per-request /
  per-token, Decimal-exact) and admitted under a Postgres advisory lock against the month-to-date
  spend and per-request cap (`cost/`). See `05-cost-budget.md`.
- **Pre-spend audit** — a readiness audit (`audit/`) gates paid operations; `--strict` exits non-zero
  on any failure. See `12-audit-readiness.md`.
- **Execution surfaces** — serverless (queue + load-balanced), public OpenAI-compatible
  endpoints, and pod leases with readiness probing and TTL/auto-teardown (`runpod_client/`,
  `leases/`). See `06-leases.md`, `08-runpod-integration.md`.
- **Webhooks** — idempotent inbound RunPod callbacks (opt-in HMAC) + signed outbound delivery
  (`webhook_receiver/`, `webhook_dispatcher/`). See `09-webhooks.md`.
- **Reconciliation** — converges workload state and applies terminal status
  (`reconciler/`, `workload_lifecycle.py`). See `10-reconciler-lifecycle.md`.
- **Safety & ops** — rate limiting, kill-switch, retention windows, R2 staging cleanup
  (`rate_limits/`, `ops/`, `retention/`). See `11-rate-limiting.md`, `15-operations.md`.
- **Observability & cost export** — Prometheus metrics + a cost exporter service
  (`observability/`, `cost_exporter/`). See `13-observability.md`.

## Surfaces

- **REST API** (`pitwall-api`) — capability/provider CRUD, inference, leases, jobs, OpenAI proxy,
  admin (audit-capability, kill-switch). `/v1/admin/*` is gated by a constant-time secret. See
  `02-api-rest.md`.
- **MCP server** (`pitwall-mcp`) — the same service layer exposed as Model Context Protocol tools.
  See `03-mcp-server.md`.
- **Background services** — `pitwall-reconciler`, `pitwall-webhook`, `pitwall-cost-exporter`.
- **CLI** (`pitwall-gpu-broker`) — operational commands. See `18-cli.md`.

## Technology

Python 3.12–3.13 · FastAPI / Starlette · asyncpg (Postgres) · redis / arq (queues) · Pydantic v2 ·
the RunPod SDK + REST/GraphQL · Prometheus client · boto3 (R2). Packaged with hatchling, managed
with uv. See `19-deployment.md`.

## Fail-closed posture

Each service refuses to boot (`os.EX_CONFIG`) when its required runtime variables
are absent. Non-loopback API and webhook binds additionally require their
authentication credentials. See `16-core-config.md` and `14-security.md`.

## Quality program

Pitwall carries unit, property, integration, concurrency, chaos, security,
mutation, performance, and public-alpha release-readiness lanes. See
`17-testing-strategy.md`.

## Document map

| Doc | Subsystem |
|-----|-----------|
| `01-architecture.md` | System architecture & data flow |
| `02-api-rest.md` | REST API surface |
| `03-mcp-server.md` | MCP server & tools |
| `04-routing.md` | Capability routing & provider resolution |
| `05-cost-budget.md` | Cost estimation & budget admission |
| `06-leases.md` | Pod-lease lifecycle |
| `07-data-model-db.md` | Data model, schema & persistence |
| `08-runpod-integration.md` | RunPod client integration |
| `09-webhooks.md` | Inbound & outbound webhooks |
| `10-reconciler-lifecycle.md` | Reconciler & workload lifecycle |
| `11-rate-limiting.md` | Rate limiting |
| `12-audit-readiness.md` | Pre-spend audit & 16-check readiness |
| `13-observability.md` | Observability & cost export |
| `14-security.md` | Security model |
| `15-operations.md` | Operations, kill-switch & retention |
| `16-core-config.md` | Core models & configuration |
| `17-testing-strategy.md` | Testing strategy |
| `18-cli.md` | Command-line interface |
| `20-provider-plugins.md` | Provider plugins |
| `21-autopilot.md` | Autonomous Autopilot |
| `22-recommendations.md` | Recommendations |
