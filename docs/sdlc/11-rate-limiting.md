# Rate Limiting Subsystem

## 1. Purpose & Scope

The outbound rate limiting subsystem implements a **token-bucket** admission scheme for RunPod endpoint operations. It guards every outbound HTTP call to RunPod serverless endpoints (`/run`, `/runsync`, `/run/pool`) against exhausting RunPod's own per-endpoint quotas. The subsystem is invoked by the RunPod client library (`pitwall.runpod_client.*`) before each outbound request; it is never invoked by API routes or background workers directly.

Current source also has a separate inbound REST-edge limiter in `src/pitwall/api/app.py`. It is installed as middleware and is inert unless `PITWALL_INBOUND_RATE_LIMIT` parses successfully (`src/pitwall/api/app.py:137`, `src/pitwall/api/app.py:149`, `src/pitwall/api/app.py:222`, `src/pitwall/api/app.py:292`).

The outbound token-bucket path has two concerns:

- **Local admission control** — the in-process token bucket decides whether to allow a request to proceed immediately, sleep and retry, or raise `RateLimited` (503).
- **Persistent bucket state** — a PostgreSQL store keeps bucket state across process restarts so that a new Pitwall instance does not burst RunPod with requests that "forgot" recent 429s.

---

## 2. Components

### `pitwall/rate_limits/algorithm.py`

Houses all token-bucket mathematics and the `RateLimiter` orchestration class. No I/O lives here.

#### Constants

| Name | Value | Meaning |
|---|---|---|
| `REFILL_WINDOW_S` | `10.0` | Seconds to fully refill a bucket (refill rate = `capacity / 10`) |
| `LOCAL_WAIT_LIMIT_S` | `30.0` | Max seconds a caller will sleep before raising `RateLimited` |
| `CAPACITY_REFRESH_INTERVAL_S` | `30.0` | Unused in `algorithm.py`; present for future sliding-window configs |
| `CAPACITY_REBUILD_WINDOW_S` | `60.0` | Seconds over which a halved capacity linearly rebuilds to target |

#### `RateLimitConfig` (frozen dataclass)

```python
@dataclass(frozen=True)
class RateLimitConfig:
    base_limit: int                           # RunPod's documented base RPM
    per_worker_limit: int                     # Per-worker RPM allowance
    refill_window_s: float = 10.0
    max_local_wait_s: float = 30.0
    capacity_refresh_interval_s: float = 30.0
    capacity_rebuild_window_s: float = 60.0
```

Invariant: all numeric fields must be positive; `per_worker_limit` and `max_local_wait_s` may be zero. Enforced in `__post_init__`.

#### `dynamic_capacity(*, base_limit, worker_count, per_worker_limit) -> int`

Returns `max(base_limit, worker_count * per_worker_limit)`. Keeps the base burst capacity when `worker_count == 0` to avoid divide-by-zero during cold starts.

#### `halved_capacity(capacity: int) -> int`

Returns `max(1, capacity // 2)`. Used after every downstream 429 to conservatively reduce the bucket.

#### `capacity_after_429(*, target_capacity, recent_429_at, now, rebuild_window_s=60.0) -> int`

If no 429 has been recorded (`recent_429_at is None`), returns `target_capacity` unchanged. Otherwise, returns the linearly-interpolated value between `halved_capacity(target_capacity)` and `target_capacity` based on elapsed time since the 429. After `rebuild_window_s` seconds, returns `target_capacity` exactly.

#### `refill_tokens(*, tokens, capacity, elapsed_s, refill_window_s=10.0) -> float`

Refill formula: `tokens + elapsed_s * (capacity / refill_window_s)`, clamped to `capacity`.

#### `seconds_until_available(*, tokens, capacity, tokens_needed=1.0, refill_window_s=10.0) -> float`

Inverts the refill formula: `(tokens_needed - tokens) / (capacity / refill_window_s)`. Returns `0.0` if tokens are already available, `math.inf` if `tokens_needed > capacity`.

#### `TokenBucket` (dataclass)

In-memory bucket used by deterministic algorithm tests and by the inbound middleware's per-process buckets. The outbound production limiter is `TokenBucketRateLimiter` (`src/pitwall/rate_limits/algorithm.py:205`, `src/pitwall/rate_limits/algorithm.py:287`, `src/pitwall/api/app.py:218`, `src/pitwall/api/app.py:259`).

```python
@dataclass
class TokenBucket:
    capacity: int
    tokens: float | None = None
    last_refilled_at_s: float | None = None
    refill_window_s: float = 10.0
```

Key methods:
- `refill(*, now_s=None) -> float` — refills in-place using wall `time.monotonic()`
- `try_consume(tokens=1.0, *, now_s=None) -> bool` — refills then consumes atomically
- `resize(capacity, *, now_s=None) -> None` — refills then changes capacity, clamping tokens
- `retry_after_s(tokens=1.0) -> float` — seconds until `tokens` are available

#### `TokenBucketRateLimiter` (production class)

```python
class TokenBucketRateLimiter:
    def __init__(
        self,
        store: RateBucketStoreProtocol,
        config: RateLimitConfig,
        *,
        sleep: SleepFunc = asyncio.sleep,
        monotonic: MonotonicClock = time.monotonic,
        wall_clock: WallClock = _utc_now,
    ) -> None: ...
```

**`acquire(endpoint_id, operation, *, worker_count, tokens=1.0) -> RateBucket`**

1. Calls `dynamic_capacity(worker_count)` to get the target capacity.
2. Calls `_ensure_bucket_capacity(endpoint_id, operation, capacity)` — loads or creates the bucket, then adjusts its persisted capacity using `capacity_after_429` if a 429 was recently recorded.
3. Enters a spin loop calling `store.atomic_refill_consume(endpoint_id, operation, tokens)`.
4. If admitted, returns the updated `RateBucket`.
5. If rejected, computes `retry_after_s` and compares it to the remaining local wait budget (`max_local_wait_s`). If the wait would exceed the budget, raises `RateLimited`. Otherwise sleeps and retries.

**`record_429(endpoint_id, operation, *, worker_count) -> RateBucket`**

Calls `dynamic_capacity(worker_count)` to get the target, then calls `store.record_429(endpoint_id, operation, halved_capacity(target_capacity))`, which halves the *current* persisted capacity, clamps tokens, and stamps `recent_429_at`.

**`_ensure_bucket_capacity(endpoint_id, operation, capacity) -> None`**

Loads/creates the bucket, evaluates `capacity_after_429`, and calls `store.update_capacity` if the effective capacity diverges from the current persisted capacity.

---

### `pitwall/rate_limits/store.py`

Async PostgreSQL repository backed by `asyncpg`. All mutating operations run inside a transaction with `SELECT ... FOR UPDATE` (row-level lock) to prevent race conditions between concurrent workers.

#### `_bucket_from_row(row: asyncpg.Record) -> RateBucket`

Maps a DB row to the `RateBucket` model. Used by every query.

#### `_REFILL_RATE_DIVISOR = 10.0`

The refill rate in the database is always `capacity / 10` seconds — identical to the in-process `REFILL_WINDOW_S`. This constant documents that invariant.

#### `RateBucketStore.__init__(pool: asyncpg.Pool)`

Stores the connection pool; all operations acquire a connection from it.

#### `load(endpoint_id, operation) -> RateBucket | None`

```sql
SELECT endpoint_id, operation, capacity, tokens,
       last_refilled_at, recent_429_at
FROM pitwall.rate_buckets
WHERE endpoint_id = $1 AND operation = $2
```

Returns `None` if the row does not exist.

#### `create(endpoint_id, operation, capacity, *, tokens=None, last_refilled_at=None, recent_429_at=None) -> RateBucket`

Inserts a new row. Tokens default to `float(capacity)` (full bucket). Uses `ON CONFLICT DO NOTHING`; raises `asyncpg.UniqueViolationError` if the bucket already exists.

#### `load_or_create(endpoint_id, operation, capacity) -> RateBucket`

```sql
INSERT INTO pitwall.rate_buckets
    (endpoint_id, operation, capacity, tokens, last_refilled_at, recent_429_at)
VALUES ($1, $2, $3, $4, $5, NULL)
ON CONFLICT (endpoint_id, operation) DO UPDATE
SET endpoint_id = EXCLUDED.endpoint_id   -- returns the existing row
RETURNING *
```

Tokens and `last_refilled_at` are reset to full/now on conflict. This is the entry-point called by the rate limiter on every `acquire()` call to ensure a bucket exists before attempting `atomic_refill_consume`.

#### `atomic_refill_consume(endpoint_id, operation, tokens_to_consume) -> tuple[RateBucket, bool]`

```sql
SELECT ... FROM pitwall.rate_buckets WHERE endpoint_id=$1 AND operation=$2 FOR UPDATE
-- inside transaction --
UPDATE pitwall.rate_buckets SET tokens=$3, last_refilled_at=$4 WHERE ...
RETURNING *
```

Atomically refills based on `elapsed_seconds * (capacity / 10)` and attempts to consume `tokens_to_consume`. Returns `(updated_bucket, allowed)`. The `FOR UPDATE` lock prevents concurrent requests from oversubscribing the bucket.

#### `record_429(endpoint_id, operation, new_capacity=None) -> RateBucket`

Inside a `FOR UPDATE` transaction: fetches the current row, halves the capacity (or uses `min(halved, new_capacity)` if provided), refills tokens under the *old* capacity, clamps to the reduced capacity, stamps `recent_429_at = now`.

#### `update_capacity(endpoint_id, operation, new_capacity) -> RateBucket`

Inside a `FOR UPDATE` transaction: refills tokens under the old capacity, clamps to `new_capacity`, updates capacity and `last_refilled_at`.

#### `persist(bucket: RateBucket) -> RateBucket`

Overwrites all mutable fields of the persisted row. Used by callers who hold an in-process `RateBucket` model and need to sync it back.

---

### `pitwall/rate_limits/retry_after.py`

HTTP `Retry-After` header parser. RunPod may return this header on 429 responses.

#### Constants

| Name | Value | Meaning |
|---|---|---|
| `DEFAULT_MAX_RETRY_AFTER_DELAY_S` | `60.0` | Upper bound on parsed delay; prevents unbounded waits |

#### `parse_retry_after(value: str | None, *, now=None, max_delay_s=60.0) -> float | None`

Accepts:
- An integer or float string (e.g. `"30"` → `30.0`)
- An HTTP-date string (e.g. `"Thu, 28 May 2026 12:00:04 GMT"`)
- Any other string → returns `None`

Returns `None` for empty strings, unparseable values, or non-finite results. Clamps the result to `[0.0, max_delay_s]`.

---

## 3. Token-Bucket Algorithm

### Refill math

The refill rate is `capacity / REFILL_WINDOW_S` tokens per second (with `REFILL_WINDOW_S = 10.0`). A full bucket of 20 tokens fully refills in 10 seconds at 2 tokens/second.

```
new_tokens = min(capacity, tokens + elapsed_s * (capacity / refill_window_s))
```

### Capacity model

RunPod's effective quota per operation is `max(base_limit, worker_count * per_worker_limit)`. Pitwall mirrors this: `dynamic_capacity()` returns that value, and the bucket is resized to it before every acquire.

### `load_or_create` store

On every `acquire()` call the limiter calls `store.load_or_create(endpoint_id, operation, target_capacity)` which:

1. Issues `INSERT ... ON CONFLICT DO UPDATE SET endpoint_id = EXCLUDED.endpoint_id RETURNING *`.
2. This reset path is deliberately aggressive: it clobbers `tokens` back to full and `last_refilled_at` to now. Any stale state from a previous Pitwall instance is discarded on first contact.
3. After loading, `_ensure_bucket_capacity` calls `capacity_after_429` to decide whether a 429-recovery capacity should apply, and issues `update_capacity` if needed.

### The 429 path

When the RunPod client receives a 429, it calls `rate_limiter.record_429(endpoint_id, operation, worker_count=...)`:

1. `dynamic_capacity()` → `target_capacity`.
2. `store.record_429(endpoint_id, operation, halved_capacity(target_capacity))`:
   - The **currently persisted** capacity is halved (not the target).
   - Tokens are refilled under the old capacity then clamped to the reduced capacity.
   - `recent_429_at` is stamped with the current wall-clock time.
3. On the next `acquire()`, `_ensure_bucket_capacity` sees `recent_429_at` is set and calls `capacity_after_429`:
   - At `elapsed = 0`: returns `halved(target_capacity)`.
   - At `elapsed = rebuild_window_s / 2`: returns `halved + (target - halved) * 0.5`.
   - At `elapsed >= rebuild_window_s`: returns `target_capacity`.
4. If the local wait budget (`max_local_wait_s = 30s`) is exhausted before tokens become available, `RateLimited` (503) is raised.

---

## 4. Inbound request rate limiting (opt-in)

This is caller-facing HTTP rate limiting, separate from the RunPod-facing token bucket above. `InboundRateLimitMiddleware` is registered on the FastAPI app with `_INBOUND_RATE_LIMIT_CONFIG` and `_PITWALL_API_TOKEN`; request-time order is outermost. See `02-api-rest.md` for the full middleware order (`src/pitwall/api/app.py:292`, `src/pitwall/api/app.py:294`, `src/pitwall/api/app.py:295`, `docs/sdlc/02-api-rest.md:91`, `docs/sdlc/02-api-rest.md:93`).

`PITWALL_INBOUND_RATE_LIMIT` defaults to `120/60s`. `off`, `disabled`, or
`none` explicitly disables it for loopback development. Invalid syntax is a
configuration error and exits fail-closed instead of silently disabling abuse
control.

Public health/probe paths pass through without consuming tokens (`src/pitwall/api/app.py:50`, `src/pitwall/api/app.py:58`, `src/pitwall/api/app.py:226`, `src/pitwall/api/app.py:227`).

The value format is `<requests>/<window>`, for example `60/60s`. The `requests` value becomes bucket capacity, and the parsed window becomes `refill_window_s`; each admitted request consumes one token (`src/pitwall/api/app.py:128`, `src/pitwall/api/app.py:130`, `src/pitwall/api/app.py:131`, `src/pitwall/api/app.py:132`, `src/pitwall/api/app.py:259`, `src/pitwall/api/app.py:260`, `src/pitwall/api/app.py:261`, `src/pitwall/api/app.py:266`).

Client buckets are keyed by `(client_ip, token_digest)`. The IP is `scope["client"][0]` or `"unknown"`; `token_digest` is the SHA-256 digest of a matching bearer token only when `PITWALL_API_TOKEN` is configured and the presented token matches. Otherwise the token component is `None` (`src/pitwall/api/app.py:271`, `src/pitwall/api/app.py:272`, `src/pitwall/api/app.py:275`, `src/pitwall/api/app.py:277`, `src/pitwall/api/app.py:278`, `src/pitwall/api/app.py:280`, `src/pitwall/api/app.py:282`, `src/pitwall/api/app.py:284`, `src/pitwall/api/app.py:285`).

When the inbound bucket is exhausted, the middleware returns HTTP 429 with body `{"detail": "rate limit exceeded"}` and a `Retry-After` header. The delay is `ceil(bucket.retry_after_s(1.0))`, floored at one second (`src/pitwall/api/app.py:238`, `src/pitwall/api/app.py:240`, `src/pitwall/api/app.py:241`, `src/pitwall/api/app.py:242`, `src/pitwall/api/app.py:269`). This header is sent to Pitwall callers; it is not the outbound RunPod `Retry-After` parser in `pitwall/rate_limits/retry_after.py` (`src/pitwall/rate_limits/retry_after.py:1`, `src/pitwall/rate_limits/retry_after.py:22`, `src/pitwall/rate_limits/retry_after.py:28`).

---

## 5. Public Interfaces

### From `pitwall.rate_limits`

```python
from pitwall.rate_limits import (
    RateLimiter,               # = TokenBucketRateLimiter
    RateLimitConfig,
    RateBucketStore,
    RateBucketStoreProtocol,  # Protocol for duck-typing in tests
    TokenBucket,               # in-memory variant for unit tests
    RateLimited,               # re-exported from pitwall.api.exceptions
    RateLimitExceeded,         # alias for RateLimited (same class)
    parse_retry_after,
    dynamic_capacity,           # = effective_capacity
    capacity_after_429,
    refill_tokens,
    seconds_until_available,
    halved_capacity,
    REFILL_WINDOW_S,
    LOCAL_WAIT_LIMIT_S,
    CAPACITY_REBUILD_WINDOW_S,
    CAPACITY_REFRESH_INTERVAL_S,
    DEFAULT_MAX_RETRY_AFTER_DELAY_S,
)
```

### `RateLimiter` (primary caller entrypoint)

```python
class RateLimiter:
    async def acquire(
        self,
        endpoint_id: str,
        operation: str,
        *,
        worker_count: int,
        tokens: float = 1.0,
    ) -> RateBucket: ...

    async def record_429(
        self,
        endpoint_id: str,
        operation: str,
        *,
        worker_count: int,
    ) -> RateBucket: ...
```

### `RateBucketStore`

```python
class RateBucketStore:
    def __init__(self, pool: asyncpg.Pool) -> None: ...

    async def load(self, endpoint_id: str, operation: str) -> RateBucket | None: ...
    async def create(self, endpoint_id: str, operation: str, capacity: int, *, ...) -> RateBucket: ...
    async def load_or_create(self, endpoint_id: str, operation: str, capacity: int) -> RateBucket: ...
    async def atomic_refill_consume(self, endpoint_id: str, operation: str, tokens_to_consume: float) -> tuple[RateBucket, bool]: ...
    async def record_429(self, endpoint_id: str, operation: str, new_capacity: int | None = None) -> RateBucket: ...
    async def update_capacity(self, endpoint_id: str, operation: str, new_capacity: int) -> RateBucket: ...
    async def persist(self, bucket: RateBucket) -> RateBucket: ...
```

### `RateBucket` model (from `pitwall.core.models`)

```python
class RateBucket(PitwallModel):
    endpoint_id: NonEmptyString
    operation: NonEmptyString
    capacity: Annotated[int, Field(gt=0)]
    tokens: Annotated[float, Field(ge=0)]
    last_refilled_at: UTCDateTime
    recent_429_at: UTCDateTime | None = None
```

---

## 6. Configuration

The outbound RunPod-facing token-bucket subsystem reads **no environment variables**. All tuning constants are hardcoded Python values in `algorithm.py` and `retry_after.py`. Callers construct `RateLimitConfig` with desired values; the `RateBucketStore` requires only a pre-configured `asyncpg.Pool`.

| Constant | Value | Used by |
|---|---|---|
| `REFILL_WINDOW_S = 10.0` | `algorithm.py:20` | `refill_tokens`, `TokenBucket`, `seconds_until_available` |
| `LOCAL_WAIT_LIMIT_S = 30.0` | `algorithm.py:21`; default in `RateLimitConfig` |
| `CAPACITY_REBUILD_WINDOW_S = 60.0` | `algorithm.py:23`; `capacity_after_429` default |
| `CAPACITY_REFRESH_INTERVAL_S = 30.0` | `algorithm.py:22`; present in `RateLimitConfig` but unused |
| `DEFAULT_MAX_RETRY_AFTER_DELAY_S = 60.0` | `retry_after.py:9`; `parse_retry_after` default; also `QueueClient._max_retry_after_s` |

The RunPod clients (`QueueClient`, `LbClient`, `ServerlessClient`) each accept a `max_retry_after_s` constructor argument (defaults to `DEFAULT_MAX_RETRY_AFTER_DELAY_S`) which is passed to `parse_retry_after`.

The inbound REST limiter is configured at API import time by
`PITWALL_INBOUND_RATE_LIMIT`; the default is `120/60s` and disabling it requires
an explicit off value.

---

## 7. Failure Modes & Error Types

### Exceptions raised by this subsystem

| Type | Condition | Raised from |
|---|---|---|
| `RateLimited` (`status_code=503`) | Local wait budget exhausted before tokens available | `TokenBucketRateLimiter.acquire` |
| `asyncpg.UniqueViolationError` | Bucket already exists on `create()` | `RateBucketStore.create` |
| `ValueError` | Non-positive capacity, negative tokens, etc. | All validation in `algorithm.py` functions |
| `RuntimeError` | Uninitialized `TokenBucket` fields | `TokenBucket._tokens()` / `._last_refilled_at_s()` (not in production path) |

### `RateLimited` error envelope

```python
exc = RateLimited(retry_after_s=10.0)
exc.to_response_body()  # {"error": "rate_limited", "retry_after_s": 10.0}
```

The inbound limiter does not raise `RateLimited`. It returns a direct HTTP 429 JSON response with `Retry-After` (`src/pitwall/api/app.py:238`, `src/pitwall/api/app.py:240`, `src/pitwall/api/app.py:241`, `src/pitwall/api/app.py:242`).

### Edge cases

- **`worker_count = 0`**: `dynamic_capacity` returns `base_limit` (not 0), preventing a zero-capacity bucket.
- **`tokens_needed > capacity`**: `seconds_until_available` returns `math.inf`, causing `acquire` to immediately raise `RateLimited` without sleeping.
- **No bucket in DB on `atomic_refill_consume`**: raises `ValueError("No bucket found for ...")` — caller must have called `load_or_create` first.
- **`parse_retry_after` on malformed HTTP date**: returns `None`; callers fall back to their own retry schedule.
- **Concurrent 429 + consume**: `record_429` uses `FOR UPDATE` to serialize with in-flight `atomic_refill_consume` calls; the lock ensures capacity is halved from the correct baseline.

---

## 8. Testing

| File | Type | What it covers |
|---|---|---|
| `tests/test_api_security_middleware.py` | API middleware | Inbound limiter threshold returns 429 + `Retry-After`, health routes bypass it, and bearer-token keying is covered (`tests/test_api_security_middleware.py:111`, `tests/test_api_security_middleware.py:123`, `tests/test_api_security_middleware.py:124`, `tests/test_api_security_middleware.py:128`, `tests/test_api_security_middleware.py:144`) |
| `tests/rate_limits/test_bucket_algorithm.py` | Unit | `dynamic_capacity`, `parse_retry_after`, `capacity_after_429`, `refill_tokens`, `seconds_until_available`, `TokenBucket` lifecycle, full `TokenBucketRateLimiter` acquire/record_429/wait-budget scenarios with a `_FakeBucketStore` |
| `tests/unit/rate_limits/test_algorithm_boundaries.py` | Unit | Boundary cases: exact-token-after-refill, one-over-available, impossible token request (`math.isinf`) |
| `tests/property/test_token_bucket_properties.py` | Property | Hypothesis-driven state-machine tests for `TokenBucket` and `dynamic_capacity` invariants |
| `tests/integration/test_rate_bucket_concurrency.py` | Integration | `test_concurrent_consume_never_oversubscribes`: 20 concurrent tasks consuming a bucket of capacity 5; `test_record_429_races_with_consumes_and_clamps_capacity`: 429 recording races with consumes |
| `tests/test_fakes.py` | Fake | `factory.rate_limited(retry_after=30)`, `fake.add_rate_limited(retry_after="2")` helpers |
| `tests/fakes/runpod.py` | Fake | `FakeRunpodFactory.rate_limited()`, `FakeRunpodFactory.add_rate_limited()` for test fixture construction |
| `tests/api/test_openai_proxy_routes.py` | Integration | `test_429_rate_limit_passed_through`: end-to-end 429 passthrough |
| `tests/runpod_client/test_queue.py` | Unit | `test_runsync_raises_on_non_rate_limit_4xx` |
| `tests/runpod_client/test_serverless.py` | Unit | `test_chat_completion_raises_on_non_rate_limit_4xx`, `test_respx_non_rate_limit_4xx_raises` |
| `tests/runpod_client/test_lb.py` | Unit | `test_lb_raises_on_non_rate_limit_4xx`, `test_respx_lb_raises_on_non_rate_limit_4xx` |

---

## 9. Dependencies

### Internal imports

| Module | What is imported | Purpose |
|---|---|---|
| `pitwall.core.models` | `RateBucket` | Domain model for persisted bucket state |
| `pitwall.api.exceptions` | `RateLimited` | Exception raised on wait-budget exhaustion |
| `pitwall.runpod_client.queue` | `RateLimitFailure` | 429 event record (not imported by rate_limits itself; the client uses `parse_retry_after`) |
| `pitwall.runpod_client.lb` | — | Uses `parse_retry_after` from retry_after submodule |
| `pitwall.runpod_client.serverless` | — | Uses `parse_retry_after` from retry_after submodule |

### External libraries

| Library | Where used |
|---|---|
| `asyncpg` | `store.py` — all PostgreSQL operations |
| `pydantic` | `core.models.RateBucket` — domain model base |
| Python standard library: `asyncio`, `datetime`, `math`, `time`, `email.utils` | `algorithm.py`, `retry_after.py` |
