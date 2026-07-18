# Deployment and Packaging

## 1. Supported deployment shape

The public alpha supports one self-hosted Compose topology. `docker-compose.yml` is canonical;
`docker-compose.prod.yml` is a compatibility include of that file. PostgreSQL and Redis are
internal-only dependencies. The API, webhook receiver, and cost exporter bind to loopback on the
host by default.

```text
                              egress to RunPod / webhook destinations
                                            ▲
                                            │
PostgreSQL ◄── migrate ◄── api ─────────────┤
     ▲                    reconciler ───────┤
     ├──────────────────── webhook ─────────┤
     └──────────────────── cost-exporter ───┘

Redis ◄──────────────────── api / reconciler / webhook
```

The one-shot `migrate` service must finish successfully before application services start. The
database migration runner serializes concurrent processes with a PostgreSQL advisory lock and
rejects checksum drift in already-applied migrations.

MCP uses local stdio and is launched by the MCP client. It is intentionally absent from the
background Compose stack because a stdio server exits when its client closes. Network MCP
transports are rejected for the public alpha.

## 2. Release images

| Image | Dockerfile | Runtime | Interface |
| --- | --- | --- | --- |
| `ghcr.io/buckeyes22/pitwall-gpu-broker/api` | `docker/Dockerfile.api` | `python -m pitwall.api` | HTTP 8080 |
| `ghcr.io/buckeyes22/pitwall-gpu-broker/reconciler` | `docker/Dockerfile.reconciler` | `python -m pitwall.reconciler` | Headless Arq worker |
| `ghcr.io/buckeyes22/pitwall-gpu-broker/webhook` | `docker/Dockerfile.webhook` | `python -m pitwall.webhook_receiver` | HTTP 8082 |
| `ghcr.io/buckeyes22/pitwall-gpu-broker/cost-exporter` | `docker/Dockerfile.cost-exporter` | `python -m pitwall.cost_exporter` | HTTP 9109 |
| `ghcr.io/buckeyes22/pitwall-gpu-broker/mcp` | `docker/Dockerfile.mcp` | `python -m pitwall.mcp` | Local stdio |

There is no project-supplied GPU/vLLM worker image. Operators provide and review the image used by
RunPod pod leases. See [ADR 0002](../decisions/0002-worker-deferred.md).

Every service Dockerfile:

- pins the Python base image by digest;
- exports the frozen lock to hash-checked requirements;
- builds a wheel and installs it into a clean runtime stage;
- runs as numeric UID/GID `10001:10001`;
- defines an OCI license label and a service health check.

The canonical Compose stack additionally enables an init process, a read-only root filesystem,
bounded `/tmp` tmpfs, `no-new-privileges`, and an empty Linux capability set. Resource limits are
declared for each application service, and Uvicorn entry points enforce bounded concurrency
(API 100, webhook 50, exporter 20 by default). Host ports bind to `PITWALL_BIND_IP`, which defaults to
`127.0.0.1`; deliberately setting a non-loopback address makes the operator responsible for the
surrounding network boundary.

## 3. Starting the stack

Copy `.env.example` to an operator-controlled environment file, replace every placeholder, and
then validate the rendered configuration before startup:

```bash
docker compose config -q
docker compose up -d --build --wait --wait-timeout 180
curl --fail http://127.0.0.1:8080/readyz
curl --fail http://127.0.0.1:8082/readyz
curl --fail http://127.0.0.1:9109/readyz
```

The API and webhook receiver readiness endpoints validate PostgreSQL and configured Redis;
the exporter readiness endpoint validates PostgreSQL. `/healthz` is process liveness only.
Unauthenticated application routes must return 401 when API auth is enabled.

Shutdown is cooperative:

```bash
docker compose stop -t 10
docker compose down
```

Keep named volumes during routine restarts. `docker compose down --volumes` deletes database,
Redis, and retention-archive data and is appropriate only for a deliberate disposable deployment.

## 4. Required configuration

The canonical stack uses Compose required-value interpolation for its security-sensitive inputs.
The complete example and comments live in `.env.example`; principal requirements are:

| Variable | Consumers | Purpose |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL, app DSNs | Database authentication |
| `REDIS_PASSWORD` | Redis, app DSNs | Redis authentication |
| `RUNPOD_API_KEY` | API, reconciler | RunPod outbound credential |
| `PITWALL_API_TOKEN` | API | All-scope bearer token |
| `PITWALL_API_SCOPED_TOKENS` | API | Optional token-to-scope JSON mapping |
| `PITWALL_ADMIN_SECRET` | API | Admin-route shared secret |
| `PITWALL_WEBHOOK_SECRET` | Webhook | Inbound HMAC verification secret |
| `PITWALL_WEBHOOK_ENCRYPTION_KEYS` | API, reconciler | Versioned AES-GCM key mapping for stored webhook secrets |
| `PITWALL_ARCHIVE_ENCRYPTION_KEY` | Reconciler | URL-safe base64 32-byte retention archive key |

Never commit a populated environment file. Database tools pass passwords through libpq environment
variables rather than process arguments. Logs use process-wide redaction for configured secrets,
authorization headers, and credential-bearing URLs.

## 5. Data and lifecycle

Named volumes are used for PostgreSQL, Redis persistence, and encrypted retention archives.
The reconciler applies archive/purge retention according to `PITWALL_RETENTION_*`; archive files
are AES-GCM protected and recorded in transactional retention-run evidence before source deletion.

The backup drill uses PostgreSQL custom-format `pg_dump`/`pg_restore`, restores the complete source
schema into an isolated database, and compares the row count and order-independent SHA-256 content
digest of every base table. See [operations](15-operations.md) and
[upgrade/recovery](../operator/upgrade-recovery.md).

## 6. Python artifacts and entry points

The distribution name and primary CLI are `pitwall-gpu-broker`. The wheel and sdist include all SQL
migrations as package resources and support Python 3.12 and 3.13. The six console entry points are
`pitwall-gpu-broker`, `pitwall-api`, `pitwall-mcp`, `pitwall-reconciler`, `pitwall-webhook`, and
`pitwall-cost-exporter`.

Release verification installs both the wheel and sdist outside the checkout, checks the reported
version and entry points, confirms the migration resource inventory, and runs migration/status
against real PostgreSQL. Reproducible builds must produce byte-identical wheel and sdist files when
`SOURCE_DATE_EPOCH` is fixed.

## 7. Release supply chain

The release workflow:

1. validates the requested prerelease tag against package and changelog versions;
2. waits for blocking release-readiness jobs;
3. builds Python artifacts twice and compares bytes;
4. installs the exact wheel and source archive in clean environments;
5. builds the five images and promotes the exact tested digests;
6. generates Python and image SBOMs;
7. scans dependencies and images at the configured severity threshold;
8. creates GitHub artifact attestations and publishes checksums with the GitHub release.

GitHub environment protection, branch rules, and the publication rehearsal are
verified on the canonical repository before tagging a release. PyPI and
TestPyPI are not part of the first public release.

## 8. Test infrastructure

`docker-compose.testinfra.yml` is not a production deployment. It exposes disposable PostgreSQL 16
on loopback port 5444 and Redis 7 on loopback port 6380 for integration tests. CI assigns a unique
Compose project and removes its volumes after each run.

```bash
make up
make test-int
make down
```

Container release verification should also check the rendered canonical Compose file, image user,
read-only/capability settings, readiness, authenticated route behavior, and cooperative shutdown.
