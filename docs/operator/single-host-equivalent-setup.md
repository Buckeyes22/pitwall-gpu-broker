# Single-Host Setup

This runbook deploys the supported public-alpha topology on one Linux host with Docker Compose.
The default is loopback-only access. A private-interface or reverse-proxy deployment is an operator
extension and must preserve authentication, TLS, and network controls.

## Prerequisites

- Docker Engine with the Compose plugin;
- enough storage for PostgreSQL, Redis, and encrypted retention archives;
- a RunPod API key with only the permissions the operator intends Pitwall to exercise;
- an operator-managed secret store or restrictive environment-file location;
- a tested backup target outside this host before production data is admitted.

Clone a reviewed tag or commit, not an unreviewed moving branch.

## Configure

```bash
cp .env.example .env
chmod 600 .env
```

Replace every placeholder used by `docker-compose.yml`. At minimum configure strong, unique values
for PostgreSQL, Redis, API bearer auth, admin auth, inbound webhook HMAC, webhook-secret
encryption, archive encryption, and RunPod authentication. Generate independent 32-byte keys for
the versioned AES-GCM settings; do not reuse passwords or tokens as encryption keys.

Keep the default host binding unless remote access is deliberately required:

```dotenv
PITWALL_BIND_IP=127.0.0.1
```

For a private interface, set `PITWALL_BIND_IP` to that host address and enforce firewall policy.
Terminate TLS at a trusted reverse proxy before any traffic crosses an untrusted network. The API
still requires bearer authorization; admin routes additionally require `X-Pitwall-Secret`.

`PITWALL_TAILSCALE_IP` is optional metadata for Tailscale-related application features. It does not
control Compose port binding. Use `PITWALL_BIND_IP` for that purpose.

## Validate and start

```bash
docker compose config -q
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
```

Expected long-running services are PostgreSQL, Redis, API, reconciler, webhook, and cost exporter.
The `migrate` container exits zero after applying migrations. MCP is local stdio and is launched by
its client; it is not a background Compose service.

Verify the supported endpoints on the bound address:

```bash
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
curl --fail http://127.0.0.1:8082/readyz
curl --fail http://127.0.0.1:9109/readyz
```

The API and webhook receiver readiness endpoints must report healthy configured dependencies;
the cost exporter readiness endpoint must report healthy PostgreSQL. Verify authentication separately:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${PITWALL_API_TOKEN}" \
  http://127.0.0.1:8080/v1/capabilities
```

An unauthenticated request to that route should return 401. Do not print secrets or the rendered
Compose environment into shared logs.

## MCP client configuration

Configure the local MCP client to execute the installed command with required environment supplied
from the operator's secret mechanism:

```json
{
  "command": "pitwall-mcp",
  "env": {
    "PITWALL_MCP_TRANSPORT": "stdio",
    "DATABASE_URL": "<operator supplied>",
    "REDIS_URL": "<operator supplied>",
    "RUNPOD_API_KEY": "<operator supplied>"
  }
}
```

Do not use SSE or streamable HTTP; the public alpha rejects network MCP until an authenticated
transport exists.

## Operations

- Review `docker compose logs` through a secret-aware logging sink.
- Monitor API readiness and cost-exporter metrics.
- Retain old archive keys for any ciphertext that still references their key versions.
- Run the real backup/restore drill on schedule and keep evidence outside ephemeral container
  storage.
- Exercise the kill switch with real Tailscale and RunPod adapters in an approved environment.
- Follow `docs/operator/upgrade-recovery.md` before version changes.

Routine restart:

```bash
docker compose pull
docker compose up -d --wait --wait-timeout 180
```

Graceful stop without deleting data:

```bash
docker compose stop -t 10
```

`docker compose down --volumes` permanently removes the deployment's database, Redis, and archive
volumes. Use it only for an explicitly disposable stack.

## Acceptance checklist

- [ ] `docker compose config -q` succeeds with no blank required secret.
- [ ] The migration container exits 0 and all long-running services become healthy.
- [ ] `/readyz` reports PostgreSQL and Redis ready.
- [ ] Unauthenticated API access is rejected and authorized access succeeds.
- [ ] Webhook signatures are required and a signed delivery succeeds.
- [ ] Host bindings and firewall match the intended trust boundary.
- [ ] Backup/restore and encrypted-retention evidence is current.
- [ ] Live RunPod and kill-switch acceptance is recorded outside the repository.
