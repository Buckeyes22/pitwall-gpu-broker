# Dogfood Acceptance Checklist (E7-S7)

**Acceptance test for standing up Pitwall from the public repo + public docs
alone, with no insider shortcuts.** Every item must pass before the public artifact
is considered fit for launch.

---

## Prerequisites

- `git`, `uv` (Python package manager), Docker, and network access.
- A clean working directory (this runbook creates temporary files under `/tmp`).
- **Do not source any internal-only environment files.** Use only `.env.example`
  and the values documented in this checklist.

---

## Step 1 — Fresh clone

- [ ] Clone the public repository into a clean temporary directory.

```bash
tmpdir="$(mktemp -d)"
git clone https://github.com/buckeyes22/pitwall-gpu-broker.git "$tmpdir/pitwall-public"
cd "$tmpdir/pitwall-public"
```

Expected: clone succeeds with no error. The directory contains `pyproject.toml`,
`src/`, `docs/`, and `tools/`.

- [ ] Confirm exactly one public commit exists and no private remote is present.

```bash
test "$(git rev-list --all --count)" = '1'
git remote -v
# origin should point only at github.com/buckeyes22/pitwall-gpu-broker.git
```

Expected: `1` commit; `git remote -v` shows only the public GitHub remote.

---

## Step 2 — Dependency installation

- [ ] `uv sync` with the `dev` extra (verifies all optional dependencies resolve).

```bash
uv sync --extra dev
```

Expected: all packages resolved and installed without error. The `.venv/`
directory is created.

- [ ] Copy `.env.example` to `.env` and fill in only the **required** values
for a local dry-run (no RunPod key with real credit, no Resend, no Langfuse,
no Tailscale).

```bash
cp .env.example .env
```

Edit `.env` and set:

```env
RUNPOD_API_KEY=local-dry-run-key
DATABASE_URL=postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test
REDIS_URL=redis://127.0.0.1:6380/0
PITWALL_ADMIN_SECRET=local-admin-secret
PITWALL_WEBHOOK_SECRET=local-webhook-secret
PITWALL_CLOUD_WORKER_IMAGE=ghcr.io/your-org/reviewed-runpod-image@sha256:<digest>
```

Leave every optional variable **empty/unset** (Resend, Langfuse, Tailscale,
R2, etc.).

Expected: `.env` is written without errors. No secret values are hard-coded
in any committed file.

---

## Step 3 — Local infrastructure

- [ ] Start the local Postgres and Redis containers from `docker-compose.testinfra.yml`.
  This is the **no-local-psql path**: a Docker-native test stack that
  requires no psql client on the host.

```bash
docker compose -f docker-compose.testinfra.yml up -d
```

Expected: `pitwall-test-postgres` and `pitwall-test-redis` containers are running and
healthy (`docker compose -f docker-compose.testinfra.yml ps` shows `(healthy)`).

- [ ] Verify database connectivity.

```bash
# pg_isready is inside the postgres container — no local psql needed
docker exec pitwall-test-postgres pg_isready -U pitwall -d pitwall_test
```

Expected: output contains `accepting connections`.

---

## Step 4 — Database migration

- [ ] Apply all migrations against the fresh database.

```bash
uv run pitwall-gpu-broker db migrate
```

Expected: each migration script reports `applied` with no error. Final line
contains `OK` or migration count matching `db/migrations/` contents.

- [ ] Confirm migration status reports zero pending.

```bash
uv run pitwall-gpu-broker db status
```

Expected: all migrations are `applied`; zero `pending`.

---

## Step 5 — Seed capability and provider

> **Seed and provider-registration dependency.** The public artifact ships with committed
> seed files in `./seed/`. The quickest path is `pitwall-gpu-broker init --non-interactive`,
> which uses those seeds automatically. The explicit manual path below
> (`register-endpoint`) is also documented for operators who prefer step-by-step
> control.

### Path A — `pitwall-gpu-broker init` (recommended)

- [ ] Run the guided onboarding command non-interactively.

```bash
uv run pitwall-gpu-broker init --non-interactive
```

Expected: `Pitwall init complete` printed to stdout. A capability and provider
are created from `seed/capabilities.yaml` and `seed/providers.yaml`, the first
provider is marked `healthy`, and a dry-run smoke `curl` command is printed.

- [ ] Verify the seeded provider is routable.

```bash
uv run pitwall-gpu-broker set-provider-health prov_demo_runpod_lb healthy
```

Expected: `Provider health updated: prov_demo_runpod_lb` with
`health_status: healthy`.

### Path B — Manual `register-endpoint`

- [ ]   Register a provider + capability using `pitwall-gpu-broker register-endpoint`.
  This exercises the real CLI command against the live DB.

```bash
uv run pitwall-gpu-broker register-endpoint \
  --endpoint-id eptest00000000 \
  --provider-type serverless_lb \
  --capability-id "cap_llm_demo" \
  --capability-name "llm.demo" \
  --name "demo-lb" \
  --region "local" \
  --gpu-class "NVIDIA L4" \
  --cost-mode per_second \
  --per-second-active 0.001 \
  --priority 1 \
  --health healthy
```

Expected: `Provider registered: prov_...` printed to stdout. The provider row
exists in `pitwall.providers` with `health_status=healthy`, so it is eligible
for routing immediately. If you register with the default `unknown` status,
run `uv run pitwall-gpu-broker set-provider-health <provider-id> healthy` after verifying
the endpoint.

- [ ] If you registered without `--health healthy`, mark the provider healthy
  via the CLI. Use the provider id printed by `register-endpoint`; no local
  `psql` client is required.

```bash
uv run pitwall-gpu-broker set-provider-health prov_... healthy
```

Expected: `Provider health updated: prov_...` with `health_status: healthy`.

---

## Step 6 — Successful inference (dry-run)

- [ ] Start the API server in the background.

```bash
uv run pitwall-api &
```

Expected: uvicorn starts on `127.0.0.1:8080`; `curl http://127.0.0.1:8080/healthz`
returns `{"status":"ok"}`.

- [ ] POST a dry-run inference request. This exercises the full routing,
  cost-gate, and audit path without calling RunPod or spending money.

```bash
curl -s -X POST "http://127.0.0.1:8080/v1/inference" \
  -H "Content-Type: application/json" \
  -d '{
    "capability": "llm.demo",
    "model": "test-model",
    "messages": [{"role": "user", "content": "hello"}],
    "dry_run": true
  }' | jq .
```

Expected: `200 OK`. Response contains `"dry_run": true` and a non-null
`"selected_provider_id"` matching the provider registered in Step 5.

- [ ] Verify the capability list endpoint returns the seeded capability.

```bash
curl -s http://127.0.0.1:8080/v1/capabilities | jq '.[].name'
```

Expected: output contains `"llm.demo"` (or `"embedding.demo"` if you used
`pitwall-gpu-broker init` with the default seeds).

- [ ] Stop the background API process.

```bash
kill %1 2>/dev/null || true
```

---

## Step 7 — Cost estimation and budget gate

> The cost subsystem must be exercisable without live RunPod calls or real
> spend. Dry-run inference already hits the estimator; this step verifies the
> budget gate and cost-exporter seams are present and do not crash when
> optional backends are absent.

- [ ] Verify the audit capability endpoint returns a cost estimate for the
  seeded capability.

```bash
curl -s -X POST "http://127.0.0.1:8080/v1/admin/audit-capability/llm.demo" \
  -H "Content-Type: application/json" \
  -H "X-Pitwall-Secret: local-admin-secret" \
  -d '{"provider_id":"prov_demo_runpod_lb","dry_run":true}' | jq .
```

Expected: `200 OK`. Response contains `passed: true` and a non-null
`cost_estimate_usd`.

- [ ] Verify the cost exporter starts and exposes Prometheus metrics without
  crashing when Redis is unavailable.

```bash
python3 - <<'PY'
import os
os.environ["DATABASE_URL"] = "postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test"

from pitwall.cost.exporter import _refresh
from fastapi import FastAPI

app = FastAPI()
# _refresh polls the DB and sets gauges; it must not raise
_refresh(app)
print("cost_exporter_refresh_ok")
PY
```

Expected: prints `cost_exporter_refresh_ok` with no exception.

- [ ] Verify the budget gate admits a dry-run request with a negligible cost.

```bash
python3 - <<'PY'
import asyncio, os
from decimal import Decimal

os.environ["DATABASE_URL"] = "postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6380/0"

from pitwall.cost.budget_gate import BudgetGate
from pitwall.db import get_pool

async def _test():
    pool = await get_pool()
    gate = BudgetGate(pool)
    admitted, reason = await gate.admit(Decimal("0.0001"))
    print(f"admitted={admitted} reason={reason}")
    await pool.close()

asyncio.run(_test())
PY
```

Expected: `admitted=True` with `reason=None` (or `reason="under_budget"`).
No exception raised.

---

## Step 8 — Kill-switch fires with NoOpNetworkSever

> Out-of-the-box (no `TAILSCALE_*` env vars set), `emergency.run_kill()`
> constructs `NoOpNetworkSever` instead of `TailscaleNetworkSever`. The
> kill-switch still terminates RunPod compute and writes the kill-log;
> it does **not** crash or raise `ValueError`.

- [ ] Verify `NoOpNetworkSever` is selected when Tailscale env vars are absent.

```bash
python3 - <<'PY'
import os

# Confirm all Tailscale vars are absent (as they should be from Step 2)
os.environ.pop("TAILSCALE_OAUTH_CLIENT_ID", None)
os.environ.pop("TAILSCALE_OAUTH_CLIENT_SECRET", None)
os.environ.pop("TAILSCALE_TAILNET", None)

from pitwall.api.admin.emergency import _build_network_sever
sever = _build_network_sever()
print(type(sever).__name__)
PY
```

Expected: prints `NoOpNetworkSever`.

- [ ] Activate the kill-switch and verify it succeeds (no tailnet required).

```bash
python3 - <<'PY'
import asyncio, os
os.environ["DATABASE_URL"] = "postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6380/0"

from pitwall.api.admin.emergency import run_kill
from pitwall.db import get_pool

async def _test():
    pool = await get_pool()
    report = await run_kill(reason="dogfood-test", actor="checklist", terminate_compute=False)
    print(f"kill_switch activated: tag={report.tag}")
    print(f"network_severed={report.network_severed}")
    print(f"tailscale_acl_updated={report.tailscale_acl_updated}")
    await pool.close()

asyncio.run(_test())
PY
```

Expected: `kill_switch activated` printed. `network_severed` is `False`
(compatibility field). `tailscale_acl_updated` is `False`
(the legacy field preserved until the rename migration). No exception raised.

---

## Step 9 — Alerts fire via LogNotifier; tracing is inert

- [ ] Verify `LogNotifier` is the active notifier when `RESEND_API_KEY` is unset.

```bash
python3 - <<'PY'
from pitwall.cost.notifications import get_notifier
notifier = get_notifier()
print(type(notifier).__name__)
PY
```

Expected: prints `LogNotifier`.

- [ ] Verify `LogNotifier.send()` logs without error and does not call Resend.

```bash
python3 - <<'PY'
import logging, io
log_stream = io.StringIO()
handler = logging.StreamHandler(log_stream)
logging.root.addHandler(handler)
logging.root.setLevel(logging.INFO)

from pitwall.cost.notifications import LogNotifier
notifier = LogNotifier()
notifier.send(
    subject="Dogfood checklist alert test",
    body="This alert should appear in the log output below.",
    recipients=["checklist@example.com"],
)
print(log_stream.getvalue())
PY
```

Expected: alert subject and body appear in the log output. No `resend` module
is imported, no HTTP request is made to the Resend API.

- [ ] Verify Langfuse tracing is inert when `LANGFUSE_SECRET_KEY` is unset.

```bash
python3 - <<'PY'
import os
os.environ.pop("LANGFUSE_HOST", None)
os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
os.environ.pop("LANGFUSE_SECRET_KEY", None)

from pitwall.observability import start_inference_trace, emit_inference_trace
trace = start_inference_trace("llm.demo", "msg_001")
print(f"trace started: {trace is not None}")
# Emit with no Langfuse key — must not raise
emit_inference_trace(trace, "llm.demo", "msg_001", {"choices": [{"message": {"content": "ok"}}]}, 0.0)
print("trace emitted without error")
PY
```

Expected: `trace started: True` (trace object created) followed by
`trace emitted without error`. No HTTP call to Langfuse, no `LANGFUSE*`
environment variable check throws.

---

## Step 10 — No internal markers leak

- [ ] Run `tools/guards/repo_text_policy.py` over every tracked file.

```bash
git ls-files -z | xargs -0 python tools/guards/repo_text_policy.py
echo "exit code: $?"
```

Expected: zero matches (exit code `0`). The guard must not report any
leak-marker findings (real RunPod endpoints, internal hosts, or org handles)
in committed source.

- [ ] Final scrub grep is zero (catch-all for any PII/organizational marker
  that the guard script does not yet cover).

```bash
# The guard's denylist is the source of truth for known leak markers:
python tools/guards/repo_text_policy.py $(git ls-files)
```

Expected: the guard exits `0`. For an extra manual sweep, grep
case-insensitively for *your own* organization's host / handle / account-id
literals, plus any non-loopback `IP:port` or real `*.proxy.runpod.net` /
`api.runpod.ai/v2/<id>` URLs. Those literal values are intentionally **not**
embedded in this published checklist, so the checklist itself stays publishable.

---

## Step 11 — Teardown

- [ ] Stop all running Pitwall services and local infrastructure.

```bash
# Stop background API / reconciler / webhook / cost-exporter processes
kill %1 %2 %3 %4 2>/dev/null || true

# Stop test infrastructure
docker compose -f docker-compose.testinfra.yml down
```

Expected: all Docker containers stop cleanly. No orphaned volumes remain
unless you intentionally keep them for the reconnect steps.

- [ ] Verify teardown idempotency: running `down` a second time exits 0.

```bash
docker compose -f docker-compose.testinfra.yml down
```

Expected: `exit code 0`. No error about missing containers or networks.

---

## Launch gate

**Launch is blocked until every box in every step is checked.**

A failure in any step is a launch-blocking bug that must be fixed in the
public artifact before the alpha cut. The maintainer running this checklist
must not use any insider path (`pip install` from a private index, direct
DB inserts, pre-seeded config, etc.) to work around a failure — if a step
fails, the artifact fails; fix the artifact.

---

## Quick reference

| Step | Command | Key prereq |
| --- | --- | --- |
| 1 | `git clone` | — |
| 2 | `uv sync --extra dev` | `uv` |
| 3 | `docker compose -f docker-compose.testinfra.yml up -d` | Docker |
| 4 | `uv run pitwall-gpu-broker db migrate` | Step 3 infra |
| 5a | `uv run pitwall-gpu-broker init --non-interactive` | `./seed/` files |
| 5b | `uv run pitwall-gpu-broker register-endpoint …` | Step 4 migration |
| 6 | `POST /v1/inference` (dry_run) | Steps 4–5 |
| 7 | `POST /v1/admin/audit-capability/{name}` | Admin secret |
| 8 | `emergency.run_kill()` | NoOpNetworkSever |
| 9 | `get_notifier()`, `start_inference_trace()` | LogNotifier default |
| 10 | `repo_text_policy.py` | tracked files |
| 11 | `docker compose … down` | — |
