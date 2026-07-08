# Inbound & Outbound Webhooks

## 1. Purpose & Scope

The webhook subsystem handles two opposing directions:

**Inbound (webhook_receiver)** — Receives HTTP callbacks from RunPod when queue jobs change state. Runs as a standalone FastAPI service (`python -m pitwall.webhook_receiver`) on port 8082. Persists every delivery attempt to `pitwall.runpod_webhook_deliveries` and, for terminal-status events, enqueues a background job to drive state reconciliation. Duplicate deliveries (same `runpod_job_id` + `attempt`) are silently ignored via a DB-level `ON CONFLICT DO NOTHING`.

**Outbound (webhook_dispatcher)** — Sends signed HTTP POSTs to consumer-registered webhook endpoints when workloads complete. Each delivery carries a timestamped HMAC-SHA256 signature and is retried up to 4 times with exponential back-off. Delivery failures are recorded separately in `pitwall.webhook_delivery_failures` so transient failures never pollute workload state.

Both directions share the same `sign`/`verify` scheme from `webhook_dispatcher.signer`.

## 2. Components

### `src/pitwall/webhook_receiver/__init__.py` — FastAPI receiver app

**Responsibility:** HTTP ingress for RunPod callbacks. Runs the FastAPI app, owns the lifespan (DB pool + optional Redis), and handles the `/webhooks/runpod` and `/runpod` POST routes.

**Key constants (lines 37-49):**

```python
_WEBHOOK_SECRET = os.environ.get("PITWALL_WEBHOOK_SECRET", "")   # empty = unauthenticated
_SIGNATURE_HEADER = "X-Pitwall-Webhook-Signature"
_RUNPOD_TERMINAL_STATUSES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED",
    "TIMED_OUT", "TIMEOUT", "TIME_OUT",
})
```

**Lifespan (lines 52-73):** Creates an `asyncpg.Pool` (min=1, max=4) from `DATABASE_URL`. Builds `arq.connections.RedisSettings` from `REDIS_URL` if present; silently continues without Redis if it's unavailable.

**Route handler `runpod_webhook` (lines 89-141):**

```python
async def runpod_webhook(request: Request) -> dict[str, Any] | JSONResponse
```

Flow:
1. Reads raw body bytes.
2. If `_WEBHOOK_SECRET` is set, extracts `X-Pitwall-Webhook-Signature` and calls `verify_signature(body, signature, _WEBHOOK_SECRET)` (delegates to `signer.verify`). Returns 401 if invalid.
3. JSON-decodes the body and rejects malformed or non-object JSON with HTTP 400.
4. Attempts `normalize_runpod_webhook(payload, headers)`. If it raises `ValueError` (no job ID found), falls through to the fallback path.
5. Calls `WebhookDeliveryRepository.insert_or_skip(runpod_job_id, attempt, payload)`.
6. If `result.is_new` and `event.status in _RUNPOD_TERMINAL_STATUSES`, calls `_enqueue_terminal_status_job(...)`.
7. Returns `{"ok": True, "duplicate": not result.is_new}`.

**Fallback path:** Used when RunPod payload doesn't parse as a `RunPodWebhookEvent`. Tries `id`, `job_id`, `jobId`, `runpod_job_id` fields, then `RunPod-Job-Id` / `X-RunPod-Job-Id` headers, then SHA-256 of the raw body as a last resort. Applies the same terminal-status enqueueing logic.

**`_enqueue_terminal_status_job`:** Uses `arq.create_pool(redis_settings)` then `enqueue_job("process_webhook_terminal_status", runpod_job_id, status)`. It returns if Redis is not configured and logs enqueue failures without exposing connection details.

**Invariant:** Valid, authenticated deliveries are acknowledged idempotently. Invalid signatures return 401; malformed bodies and over-limit requests return 400/413; rate-limited requests return 429. Duplicate deliveries (same job ID + attempt) are idempotent at the DB level.

---

### `src/pitwall/webhook_receiver/runpod.py` — Event model & normalization

**Responsibility:** Pydantic model for normalized RunPod webhook events, and a normalization function that handles RunPod's multiple ID-field conventions.

**`RunPodWebhookEvent` (lines 22-46):** Pydantic `BaseModel` with `extra="allow"`. Fields:

```python
runpod_job_id: str          # always non-empty after normalization
status: str                 # RunPod status string
attempt: int = Field(default=1, ge=1, le=3)
output: dict[str, Any] | None = None
error: str | None = None
raw: dict[str, Any] = Field(default_factory=dict)
```

**`normalize_runpod_webhook` (lines 75-102):**

```python
def normalize_runpod_webhook(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> RunPodWebhookEvent
```

Reads `id`, `job_id`, `jobId`, `runpod_job_id` (in that order) for the job ID. Reads `attempt` from payload first (1-3), then falls back to `X-RunPod-Attempt`, `X-Runpod-Attempt`, `RunPod-Attempt`, `Runpod-Attempt` headers. Raises `ValueError` if no valid job ID is found.

---

### `src/pitwall/webhook_receiver/__main__.py` — Entrypoint

```python
def main() -> None:
    port = int(os.environ.get("PITWALL_WEBHOOK_RECEIVER_PORT", "8082"))
    uvicorn.run("pitwall.webhook_receiver:app", host="0.0.0.0", port=port)
```

Binds to `0.0.0.0` on `PITWALL_WEBHOOK_RECEIVER_PORT` (default 8082).

---

### `src/pitwall/webhook_dispatcher/__init__.py` — Public API

Exports from `dispatcher` and `signer`:

```python
from pitwall.webhook_dispatcher.dispatcher import (
    DEFAULT_RETRY_DELAYS,   # (0.0, 1.0, 3.0, 9.0)
    DEFAULT_TIMEOUT_SECONDS, # 30.0
    MAX_ATTEMPTS,            # 4
    DeliveryOutcome,
    dispatch_completion,
)
from pitwall.webhook_dispatcher.signer import sign, verify
```

---

### `src/pitwall/webhook_dispatcher/dispatcher.py` — Outbound dispatcher

**`DeliveryOutcome` (lines 27-50):**

```python
class DeliveryOutcome:
    def __init__(
        self,
        success: bool,
        attempt: int,
        status_code: int | None = None,
        error_message: str | None = None,
        next_retry_at: dt.datetime | None = None,
    ) -> None:
    @property
    def should_retry(self) -> bool:
        # False if already succeeded, already at MAX_ATTEMPTS,
        # or received a 5xx (retry on 5xx only)
```

**`_send_webhook_with_retry` (lines 53-139):** Internal. Sends a single webhook with bounded retries.

```python
async def _send_webhook_with_retry(
    webhook_url: str,
    payload: dict[str, Any],
    hmac_secret: str | None,
    retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> DeliveryOutcome
```

- Converts payload to `str(payload).encode()` (not JSON bytes — raw string encoding).
- Sets `Content-Type: application/json` header.
- If `hmac_secret` is set, calls `sign(body, hmac_secret)` and sets `X-Pitwall-Signature` header.
- Iterates `delays[:MAX_ATTEMPTS]` (0.0, 1.0, 3.0, 9.0 → 4 attempts).
- On `httpx.TimeoutException`: returns `DeliveryOutcome(success=False, ...)` with `next_retry_at` set for all but the last attempt.
- On 2xx: returns `success=True`. On 4xx: returns `success=False` immediately (no retry). On 5xx: retries.

**`dispatch_completion` (lines 142-182):** Public interface.

```python
async def dispatch_completion(
    workload_id: str,
    consumer: str,
    payload: dict[str, Any],
    subscriptions: list[tuple[int, str, str | None]],
    retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
```

Iterates `subscriptions` (each is `(subscription_id, webhook_url, hmac_secret)`) and calls `_send_webhook_with_retry` for each. Returns `dict[subscription_id_str, result_dict]`.

---

### `src/pitwall/webhook_dispatcher/signer.py` — HMAC signing & verification

**`sign` (lines 16-31):**

```python
def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    # Returns "t={timestamp},v1={hmac_sha256_hexdigest}"
    if timestamp is None:
        timestamp = int(time.time())
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
```

**`verify` (lines 34-63):**

```python
def verify(body: bytes, header: str, secret: str, max_age: int = 300) -> bool:
    # Returns False if header is empty, missing t/v1 parts,
    # timestamp out of ±max_age range, or signature mismatch.
    # Uses hmac.compare_digest for constant-time comparison.
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ...
    if abs(now - timestamp) > max_age:
        return False
    expected = sign(body, secret, timestamp)
    return hmac.compare_digest(expected, header)
```

Invariant: Both sign and verify produce/validate the same message format `{timestamp}.{body}` with SHA-256 HMAC. The 300-second `max_age` prevents replay attacks.

## 3. Delivery Semantics

### Inbound idempotency

The receiver relies on a PostgreSQL UNIQUE constraint on `(runpod_job_id, attempt)` in `pitwall.runpod_webhook_deliveries`. `WebhookDeliveryRepository.insert_or_skip` emits:

```sql
INSERT INTO pitwall.runpod_webhook_deliveries
    (runpod_job_id, attempt, payload, received_at)
VALUES ($1, $2, $3, NOW())
ON CONFLICT (runpod_job_id, attempt) DO NOTHING
RETURNING id
```

Returns `WebhookDeliveryResult(is_new=True)` if a row was inserted, `is_new=False` if the conflict was skipped. The handler's response JSON includes `"duplicate": not result.is_new` so callers can distinguish first vs. repeat deliveries.

Separate attempt numbers (1, 2, 3) each get their own row — only identical `(runpod_job_id, attempt)` pairs are deduplicated.

### Inbound HMAC verification (opt-in)

When `PITWALL_WEBHOOK_SECRET` is set in the environment, every inbound request must carry a valid `X-Pitwall-Webhook-Signature` header. The receiver delegates directly to `signer.verify`:

```python
signature = request.headers.get(_SIGNATURE_HEADER, "")
if not verify_signature(body, signature, _WEBHOOK_SECRET):
    return JSONResponse(status_code=401, content={"ok": False, "detail": "..."})
```

The guard is **strict opt-in**: `PITWALL_WEBHOOK_SECRET=""` (the default) skips verification entirely.

### Outbound signing

Every outbound POST to a consumer webhook URL carries `X-Pitwall-Signature: t={timestamp},v1={hexdigest}`. The signing secret is the `hmac_secret` stored in `pitwall.webhook_subscriptions` for that subscription. If `hmac_secret` is `None`, the header is omitted (plaintext delivery).

### Outbound replay window

`signer.verify` enforces `max_age=300` seconds: a signature whose timestamp is older than ±300s from current time is rejected. This prevents replay of captured webhook payloads.

## 4. Public Interfaces

### From `pitwall.webhook_receiver`

- `app` — the FastAPI application instance (for mounting in tests)
- `require_runtime_env("webhook")` called at module load — aborts if `PITWALL_WEBHOOK_SECRET` is missing and the app is configured to require it (actually, it only checks that the "webhook" env namespace is registered; see `pitwall.config`)

### From `pitwall.webhook_dispatcher`

```python
async def dispatch_completion(
    workload_id: str,
    consumer: str,
    payload: dict[str, Any],
    subscriptions: list[tuple[int, str, str | None]],
    retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]
```

Callers pass `(subscription_id, webhook_url, hmac_secret)` tuples for each registered consumer endpoint. Returns a dict keyed by `subscription_id_str` containing `success`, `attempt`, `status_code`, `error_message`, and `next_retry_at`.

### From `pitwall.webhook_dispatcher.signer`

```python
def sign(body: bytes, secret: str, timestamp: int | None = None) -> str
def verify(body: bytes, header: str, secret: str, max_age: int = 300) -> bool
```

### Internal callers

| Caller | Calls |
|---|---|
| `pitwall.reconciler` | `apply_terminal_status_and_publish()` which may enqueue the ARQ job `process_webhook_terminal_status` |
| `pitwall.webhook_receiver` | `WebhookDeliveryRepository.insert_or_skip()`, `signer.verify` |
| `pitwall.api.routes.webhook_subscriptions` | `WebhookSubscriptionRepository.create/get/list` (data layer, not dispatcher) |

## 5. Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `PITWALL_WEBHOOK_SECRET` | `""` (empty) | Inbound HMAC secret. When set, inbound webhooks require `X-Pitwall-Webhook-Signature`. Empty = unauthenticated. |
| `PITWALL_WEBHOOK_RECEIVER_PORT` | `8082` | Port for `python -m pitwall.webhook_receiver` |
| `DATABASE_URL` | required | PostgreSQL connection string for webhook receiver |
| `REDIS_URL` | optional | ARQ Redis connection string; if absent, terminal-status job enqueuing is silently skipped |

## 6. Failure Modes & Error Types

### Inbound (webhook_receiver)

| Condition | HTTP response | Body |
|---|---|---|
| Missing/invalid HMAC signature (secret set) | 401 | `{"ok": False, "detail": "invalid or missing webhook signature"}` |
| Invalid or non-object JSON body | 400 | generic validation detail |
| No valid job ID found | 200 | falls back to SHA-based idempotency key |
| DB pool unavailable at startup | Raises `RuntimeError("DATABASE_URL is not set")` | — |
| Dependency unavailable at readiness probe | 503 | generic per-dependency status; no connection details |
| Redis enqueue fails after acknowledgement | 200 | failure is logged and reconciliation remains authoritative |

`/healthz` is process liveness. `/readyz` validates PostgreSQL and Redis when Redis is configured.

### Outbound (webhook_dispatcher)

`dispatch_completion` returns a results dict; it does not raise on delivery failure. Within `_send_webhook_with_retry`:

| Condition | Behavior |
|---|---|
| HTTP 2xx | `DeliveryOutcome(success=True)` |
| HTTP 4xx | `DeliveryOutcome(success=False)` — no retry |
| HTTP 5xx | Retry with backoff |
| `httpx.TimeoutException` | `DeliveryOutcome(success=False)` with `next_retry_at` set (or `None` on last attempt) |
| `httpx.HTTPError` | Retry with backoff; last error captured in `error_message` |

After all retries exhausted: `DeliveryOutcome(success=False, attempt=4, status_code=last_status_code, error_message=str(last_error))`.

### `signer.verify` returns `False` for

- Empty header or header without `,`
- Missing `t` or `v1` parts
- Non-integer timestamp
- Timestamp outside ±300s of current time
- HMAC digest mismatch (constant-time comparison)

## 7. Testing

| Test file | What it covers |
|---|---|
| `tests/security/test_webhook_receiver_signed.py` | Opt-in HMAC gate: valid signature accepted, missing/wrong/tampered/stale rejected. Static assertion that `runpod_webhook` delegates to `signer.verify` (not a custom comparison). |
| `tests/security/test_webhook_receiver_unauthenticated.py` | Default (no secret set): any unsigned POST returns 200, pins opt-in behavior. |
| `tests/test_webhook_duplicate_delivery_stress.py` | 100 concurrent POSTs of same payload → exactly 1 DB row. Same job_id + same attempt collapses to 1 row. Different attempts (1,2,3) each get their own row. `WebhookDeliveryRepository` directly tested with a start-gate concurrency test. |
| `tests/perf/test_webhook_fast200.py` | `/webhooks/runpod` under 50 ms median (no DB, mocked repo). |
| `tests/test_webhook_and_exporter.py` | Basic healthz/health endpoints, module import sanity. |
| `tests/api/test_webhook_subscriptions_contract.py` | API contract for `POST /v1/webhook-subscriptions` and `GET /v1/webhook-subscriptions`: 201 on create, 422 on missing required field, 200 array response on list, 405 on DELETE. |
| `tests/api/test_e2e_async_job_webhook.py` | Live RunPod queue job: posts real webhook to receiver, asserts `{"ok": True, "duplicate": False}` on first delivery and `{"ok": True, "duplicate": True}` on second identical POST. |

## 8. Dependencies

### From `src/pitwall/webhook_receiver/__init__.py`

```python
import asyncpg                    # async PostgreSQL driver
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pitwall.config import require_runtime_env
from pitwall.db.repository import WebhookDeliveryRepository
from pitwall.webhook_dispatcher.signer import verify as verify_signature
from pitwall.webhook_receiver.runpod import RunPodWebhookEvent, normalize_runpod_webhook
import uvicorn                    # for __main__.py entrypoint
```

### From `src/pitwall/webhook_receiver/runpod.py`

```python
from pydantic import BaseModel, Field   # Pydantic v2
```

### From `src/pitwall/webhook_dispatcher/dispatcher.py`

```python
import httpx          # HTTP client (AsyncClient)
from pitwall.webhook_dispatcher.signer import sign
```

### From `src/pitwall/webhook_dispatcher/signer.py`

```python
import hashlib, hmac, time   # stdlib only
```

### External runtime dependencies

| Library | Used by | Purpose |
|---|---|---|
| `asyncpg` | receiver, repository | PostgreSQL async driver |
| `fastapi` | receiver | HTTP framework |
| `uvicorn` | receiver `__main__` | ASGI server |
| `pydantic` | runpod model | Schema validation |
| `httpx` | dispatcher | Outbound HTTP POST with timeout |
| `arq` | receiver | Redis-based job queue (optional, degrades gracefully) |
