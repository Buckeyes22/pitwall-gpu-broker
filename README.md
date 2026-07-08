# Pitwall

[![CI](https://github.com/buckeyes22/pitwall-gpu-broker/actions/workflows/ci.yml/badge.svg)](https://github.com/buckeyes22/pitwall-gpu-broker/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%E2%80%933.13-blue.svg)](pyproject.toml)

Pitwall is an independent RunPod GPU broker: a Python control plane that accepts
capability-oriented requests, routes them to the best-fit RunPod provider, estimates and gates cost
before spend, executes inference or lease workflows, and reconciles outcomes through Postgres,
Redis, and background workers.

It is built for operators who need one consistent surface over RunPod serverless queue endpoints,
load-balanced serverless endpoints, public OpenAI-compatible endpoints, and raw pod leases.

## Project Status

Pitwall is pre-1.0 software preparing for its first public alpha. It is not yet a
stable or production-ready release, and APIs and configuration may change
between minor versions. The exact supported and deferred surfaces are listed in
the [public alpha support matrix](docs/support-matrix.md).

## Why Pitwall

- **One capability API** for inference, embeddings, and pod leases across multiple RunPod execution
  surfaces.
- **Pre-spend controls** with Decimal-exact cost estimation, per-request caps, monthly budget
  admission, and dry-run routes that stop before any paid RunPod call.
- **Deterministic routing** through hard-constraint filtering, health and cooldown gates, scoring,
  capacity checks for pod leases, and explicit fallback chains.
- **Operational safety** through idempotency, webhook de-duplication, rate limiting, retention
  controls, and an emergency kill-switch.
- **Observable operations** with structured workload records, Prometheus metrics, cost rollups, and
  optional Langfuse tracing.

Start with the source-grounded [system overview](docs/sdlc/00-overview.md).

## Architecture

Pitwall is a layered control plane. Requests enter through REST, MCP, the operational CLI, or a
background service, then move through routing, cost, audit, execution, and reconciliation layers.

```text
clients -> surfaces (REST API / MCP / CLI / background services)
           -> resolver -> routing.planner -> cost.estimator -> audit
                                      -> cost.budget_gate
                                      -> runpod_client / leases / webhooks
                                      -> Postgres + Redis + reconciler
```

Key runtime pieces:

| Area | What it does |
| --- | --- |
| REST API | Capability/provider registry, inference, leases, jobs, OpenAI proxy, admin routes |
| MCP server | Model Context Protocol tools over the same service layer |
| CLI | Database migrations, endpoint/template registration, pod termination, volume warm-up, MCP serve |
| Operator TUI | Read-only Textual operator console (`pitwall-gpu-broker dashboard`): Overview, Providers, Cost, Leases, Operations, Resources |
| Reconciler | Workload state convergence, health probes, cost rollups, lease expiry, idempotency GC |
| Webhook receiver | Idempotent, size/rate-bounded RunPod callbacks; HMAC is mandatory beyond loopback |
| Cost exporter | Prometheus metrics for spend, budget use, workers, kill-switch, and provider health |

Details and diagrams: [architecture](docs/sdlc/01-architecture.md).

## Quick Start

This path uses `uv`, Docker, and the first-class CLI. It creates local Postgres/Redis services,
applies migrations, runs `pitwall-gpu-broker init` from the example seed files, and finishes with a dry-run
`POST /v1/inference`. Dry-run inference exercises registry lookup and routing without live GPU
discovery or paid RunPod work.

```bash
git clone https://github.com/buckeyes22/pitwall-gpu-broker.git
cd pitwall-gpu-broker

uv sync --frozen --extra dev

docker compose -f docker-compose.testinfra.yml up -d --wait

export DATABASE_URL=postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test
export REDIS_URL=redis://127.0.0.1:6380/0
export RUNPOD_API_KEY=local-dry-run-key
export PITWALL_ADMIN_SECRET=local-admin-secret
export PITWALL_API_TOKEN=local-api-token

uv run pitwall-gpu-broker db migrate
uv run pitwall-gpu-broker init --non-interactive
```

`pitwall-gpu-broker init` defaults to the committed [`seed/capabilities.yaml`](seed/capabilities.yaml) and
[`seed/providers.yaml`](seed/providers.yaml) files when `./seed` exists. The example values are
intentionally fake placeholders:

- capability: `embedding.demo`
- provider: `demo-runpod-lb`
- endpoint id: `eptest00000000`
- provider type: `serverless_lb`
- region: `US-EXAMPLE-1`
- cost: `per_second_active=0.001`

The command creates or updates the capability and provider, then marks the provider `healthy` using
the same health path as `pitwall-gpu-broker set-provider-health`. To seed different values non-interactively,
use either `pitwall-gpu-broker init --from-seed path/to/seed-dir` or manual flags such as
`--capability-name`, `--endpoint-id`, `--provider-type`, `--region`, `--gpu-class`, and
`--per-second-active`.

In a second terminal, start the API:

```bash
uv run pitwall-api
```

Then run the smoke command printed by `pitwall-gpu-broker init`, or this equivalent command:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/inference \
  -H 'Authorization: Bearer local-api-token' \
  -H 'Content-Type: application/json' \
  -d '{"capability":"embedding.demo","texts":["hello"],"dry_run":true}'
```

Expected response includes `"dry_run":true` and `"selected_provider_id":"prov_demo_runpod_lb"`.
For real inference, replace the seed placeholder endpoint with your RunPod endpoint, keep
`RUNPOD_API_KEY` set to a real key, and send the same request without `dry_run`.

Useful onboarding commands:

```bash
uv run pitwall-gpu-broker create-capability --name embedding.demo --class embedding --cost-mode per_second
uv run pitwall-gpu-broker seed seed/capabilities.yaml seed/providers.yaml --mark-healthy
uv run pitwall-gpu-broker set-provider-health prov_demo_runpod_lb healthy
```

## Services

Pitwall exposes six console entry points in [pyproject.toml](pyproject.toml).

| Command | Role | Docs |
| --- | --- | --- |
| `pitwall-api` | REST API for capabilities, inference, leases, jobs, OpenAI proxy, and admin routes | [REST API](docs/sdlc/02-api-rest.md) |
| `pitwall-mcp` | MCP server over the same service layer | [MCP server](docs/sdlc/03-mcp-server.md) |
| `pitwall-reconciler` | Workload, lease, health, idempotency, and cost convergence loop | [Reconciler](docs/sdlc/10-reconciler-lifecycle.md) |
| `pitwall-webhook` | Inbound RunPod webhook receiver | [Webhooks](docs/sdlc/09-webhooks.md) |
| `pitwall-cost-exporter` | Prometheus cost and health metrics | [Observability](docs/sdlc/13-observability.md) |
| `pitwall-gpu-broker` | Operational CLI for DB, endpoint/template, pod, volume, and MCP commands | [CLI](docs/sdlc/18-cli.md) |

Container and deployment details are in [deployment](docs/sdlc/19-deployment.md).

## Documentation

The SDLC docs are the public technical map for the project. They are source-grounded and should be
checked against code when behavior changes.

| # | Doc | # | Doc |
| --- | --- | --- | --- |
| 00 | [Overview](docs/sdlc/00-overview.md) | 10 | [Reconciler and lifecycle](docs/sdlc/10-reconciler-lifecycle.md) |
| 01 | [Architecture](docs/sdlc/01-architecture.md) | 11 | [Rate limiting](docs/sdlc/11-rate-limiting.md) |
| 02 | [REST API](docs/sdlc/02-api-rest.md) | 12 | [Audit and readiness](docs/sdlc/12-audit-readiness.md) |
| 03 | [MCP server](docs/sdlc/03-mcp-server.md) | 13 | [Observability](docs/sdlc/13-observability.md) |
| 04 | [Routing and resolution](docs/sdlc/04-routing.md) | 14 | [Security model](docs/sdlc/14-security.md) |
| 05 | [Cost and budget](docs/sdlc/05-cost-budget.md) | 15 | [Operations](docs/sdlc/15-operations.md) |
| 06 | [Pod leases](docs/sdlc/06-leases.md) | 16 | [Core models and config](docs/sdlc/16-core-config.md) |
| 07 | [Data model and DB](docs/sdlc/07-data-model-db.md) | 17 | [Testing strategy](docs/sdlc/17-testing-strategy.md) |
| 08 | [RunPod integration](docs/sdlc/08-runpod-integration.md) | 18 | [CLI](docs/sdlc/18-cli.md) |
| 20 | [Provider plugins](docs/sdlc/20-provider-plugins.md) | 21 | [Autonomous Autopilot](docs/sdlc/21-autopilot.md) |
| 22 | [Recommendations](docs/sdlc/22-recommendations.md) | | |

## Testing and Quality

Common local checks:

```bash
uv run pytest -q -m "not integration and not slow"
docker compose -f docker-compose.testinfra.yml up -d
PITWALL_TEST_DATABASE_URL=postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test \
PITWALL_TEST_REDIS_URL=redis://127.0.0.1:6380/0 \
  uv run pytest -q -m integration
uv run pytest -q -m "security and not fuzz" tests/security
uv run pytest -q -m fuzz tests/security
```

The committed program covers unit, property, integration, API contract, concurrency, chaos,
security, mutation, performance, and release-readiness lanes. See the
[testing strategy](docs/sdlc/17-testing-strategy.md) for markers, gates, and release tiers.

## CI/CD

GitHub Actions are the public CI target:

- [ci.yml](.github/workflows/ci.yml) runs linting, type checks, security checks, tests, and
  integration coverage.
- [codeql.yml](.github/workflows/codeql.yml) runs CodeQL analysis.
- [release-readiness.yml](.github/workflows/release-readiness.yml) runs the public-alpha readiness lane.

## Configuration

The API, MCP server, and reconciler fail closed when required runtime variables
are missing.

| Env var | Required for | Purpose |
| --- | --- | --- |
| `RUNPOD_API_KEY` | API, MCP, reconciler, RunPod operations | RunPod credential used for outbound calls |
| `DATABASE_URL` | API, MCP, DB CLI, reconciler, exporter | Postgres connection string |
| `REDIS_URL` | API, MCP, reconciler | Redis/arq queue connection string |
| `PITWALL_ADMIN_SECRET` | Production admin routes | Constant-time shared secret for `/v1/admin/*` |
| `PITWALL_WEBHOOK_SECRET` | Production webhooks | HMAC secret for inbound RunPod webhook verification |
| `PITWALL_WEBHOOK_ENCRYPTION_KEYS` | Outbound webhook subscriptions | JSON mapping key versions to URL-safe base64 32-byte AES-GCM keys |
| `PITWALL_WEBHOOK_ENCRYPTION_CURRENT_KEY` | Outbound webhook subscriptions | Key version used for new and rotated signing secrets |
| `PITWALL_API_TOKEN` | Non-loopback API; recommended locally | All-scopes operator bearer token required on every non-health route |
| `PITWALL_API_SCOPED_TOKENS` | Delegated API callers | JSON object mapping opaque tokens to `read`, `spend`, `lease:mutate`, `webhook:admin`, and/or `server:admin` scopes |
| `PITWALL_INBOUND_RATE_LIMIT` | API abuse control | Defaults to `120/60s`; throttles callers with `429` + `Retry-After`; set `off` only for loopback development |
| `PITWALL_API_MAX_BODY_BYTES` | REST API | Defaults to 8 MiB and bounds fixed-length and chunked request bodies |
| `PITWALL_API_MAX_CONCURRENCY` | REST API process | Defaults to 100 in-flight Uvicorn connections/tasks |
| `PITWALL_WEBHOOK_MAX_CONCURRENCY` | Webhook process | Defaults to 50 in-flight Uvicorn connections/tasks |
| `PITWALL_COST_EXPORTER_MAX_CONCURRENCY` | Metrics process | Defaults to 20 in-flight Uvicorn connections/tasks |
| `PITWALL_CLOUD_WORKER_IMAGE` | Warm-volume and generated template flows | Operator-supplied and independently reviewed RunPod pod image; Pitwall does not publish one |

Optional settings cover budgets, tracing, R2 log staging, lease TTLs, RunPod registry auth, audit
parameters, and service ports. See [.env.example](.env.example) and
[core models and config](docs/sdlc/16-core-config.md).

## Security and Trust Model

Read the project security policy in [SECURITY.md](SECURITY.md).

Pitwall is a **single-operator** control plane, not a multi-tenant gateway. There is no tenancy or
ownership model — one operator, one RunPod key, one shared budget.

**Non-loopback API startup fails unless API and admin credentials are configured.**
`PITWALL_API_TOKEN` is the all-scopes operator token. Delegated clients can instead receive tokens
from `PITWALL_API_SCOPED_TOKENS`, restricted to read, spend, lease mutation, webhook administration,
or server administration. Missing credentials return 401; a valid token without the required scope
returns 403. Loopback-only development may run without bearer auth and logs an explicit warning.
The inbound limiter defaults to `120/60s` and can be disabled only by an explicit setting.

**Administrative operations use two gates when API bearer auth is enabled:** the bearer token must
grant `server:admin`, and `X-Pitwall-Secret` must match `PITWALL_ADMIN_SECRET`. Inbound webhook HMAC
is required whenever its receiver binds beyond loopback. The canonical Compose stack requires both
admin and webhook secrets.

**Bind to a private interface** (`127.0.0.1` default). Terminate TLS at a trusted reverse proxy
before any network exposure. Do not expose the webhook receiver or cost exporter directly to the
public internet.

**Unauthenticated MCP is restricted to local stdio.** Pitwall rejects every network MCP transport;
authenticated HTTP MCP is not an alpha feature. See [SECURITY.md](SECURITY.md#mcp-server).

## Project Layout

```text
src/pitwall/          application packages: api, mcp, routing, cost, leases, db, workers
db/migrations/        ordered SQL migrations
tests/                unit, integration, API, property, security, chaos, perf, release tests
docs/sdlc/            public source-grounded technical documentation
docker/               supported service image definitions
config/               Prometheus configuration
dashboards/           Grafana dashboard definitions
scripts/              release and mutation-score tooling
tools/                policy guards and security support files
```

## License

Pitwall is licensed under [Apache-2.0](LICENSE).

## Contributing

Contributions are welcome under the guidelines in [CONTRIBUTING.md](CONTRIBUTING.md).

## Code of Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Legal

### Trademark and Non-Affiliation

Pitwall is an independent open-source broker for use with RunPod-hosted GPU
workloads. RunPod and related marks are trademarks or registered trademarks of
their respective owners. Pitwall is not affiliated with, endorsed by,
sponsored by, or approved by RunPod. References to RunPod are made only to
identify the third-party service and API that Pitwall can interoperate with.
