# RunPod Client Integration — SDLC Design Document

## 1. Purpose & Scope

`pitwall.runpod_client` is the canonical integration layer between Pitwall and RunPod GPU cloud. It wraps every RunPod surface — Pod REST API, GraphQL market/billing API, Serverless queue API, Serverless load-balancer API, and template management — behind typed Python interfaces. No RunPod API call bypasses this package. All network I/O uses `httpx`; the SDK (`runpod`) is used only for template creation/listing compatibility. Every stateful pod operation has sync and async variants.

---

## 2. Components

### `providers/` reference adapter
`pitwall.providers` is the provider-plugin seam used by future compute providers. It is deliberately separate from `pitwall.core.models.Provider`: the core model is still the persisted fulfillment record selected by routing, while the plugin is the provider driver registered by stable id.

```python
class Provider(Protocol):
    id: str
    name: str
    credential_schema: type[pydantic.BaseModel]
    def pricing_model(capability, provider_record) -> TaggedPricingModel
    async def provision(ProvisionRequest) -> ProvisionResult
    async def status(StatusRequest) -> StatusResult
    async def reconcile(ReconcileRequest) -> ReconcileResult
    async def teardown(TeardownRequest) -> TeardownResult
```

`ProviderRegistry` registers plugins by id, rejects duplicate/blank ids, looks up providers, exposes each credential JSON schema, and validates supplied credentials through the provider's declared Pydantic schema. Credential validation errors report only provider id and failing field paths, not raw secret values.

`RunPodProvider` is the built-in reference plugin registered by `create_default_registry()` / `get_default_registry()` under id `runpod`. It delegates provision to `api.leases.launch.run_launch`, teardown to `api.leases.teardown.run_teardown`, status/reconcile to `runpod_client.pods`, and pricing to the tagged pricing union via `parse_pricing_model`. Current RunPod launch, routing, and teardown callers keep using the existing services; the adapter is additive.

`RunPodCredentials` requires `api_key: SecretStr` and optional `graphql_url` / `rest_api_url` overrides. Credential URLs must be absolute HTTP(S) URLs with no userinfo, query string, or fragment. Secrets are never accepted in URLs; RunPod API authentication remains header-only.

`VastProvider` is the first non-RunPod plugin registered by `create_default_registry()` / `get_default_registry()` under id `vast`. It proves the provider seam with a provider that does not delegate to existing RunPod services:

- `VastCredentials` requires `api_key: SecretStr`, optional `vast_api_url` (default `https://console.vast.ai/api/v0`), and `client_id` (default `"me"`). The base URL uses the same safe URL invariant as RunPod credential URLs: absolute HTTP(S), no userinfo, no query string, no fragment. Vast API keys are sent only as `Authorization: Bearer ...`; never in a URL.
- `pricing_model()` uses `PerSecondPricing`. Vast hourly money fields in provider config (`price_per_hour` / `rate_per_hour` / `dph_total` plus optional `bid_price_per_hour` / `bid_per_hour` / `min_bid`) are converted to Decimal per-second rates. The optional bid rate is preserved as `bid_rate_per_second`, so `upper_bound()` reserves against the larger of the live rate and bid ceiling.
- `provision()` accepts an `ask_id` / `offer_id`, builds a Vast create-instance body from provider `config.create` plus whitelisted request overrides, and sends `PUT /asks/{id}/`. If a bid price is configured, it is sent as Vast's `price` field in $/hour. Create payloads are encoded with Decimal money as JSON numbers rather than floats.
- `status()` reads `GET /instances/{id}/` and maps Vast states into provider-neutral `ResourceStatus`. `PREEMPTED`, outbid, interrupted, or evicted instances map to `ResourceStatus.FAILED` with `raw["pitwall_preempted"] = true` and `raw["pitwall_safe_state"] = "failed"`.
- `reconcile()` polls either requested external ids or `GET /instances/`. Preempted resources are converged to the safe persisted lease state `failed` when a pool is available.
- `teardown()` resolves the external instance id from the persisted lease row, sends `DELETE /instances/{id}/`, and closes the lease to the requested terminal state. Until the data model grows a generic external-resource id, non-RunPod lease providers store their external id in the legacy `runpod_pod_id` column.
`TogetherProvider` is the built-in inference-only plugin registered by the default registry under id
`together`. It targets Together's OpenAI-compatible `POST /v1/chat/completions` API using
`Authorization: Bearer <api_key>` and keeps secrets out of URLs. `TogetherCredentials` requires
`api_key: SecretStr`, accepts a safe `base_url` override (default `https://api.together.xyz/v1`),
and rejects URL userinfo, query strings, and fragments.

Together pricing is always per-token. `TogetherProvider.pricing_model()` requires a per-token
capability and returns the `PerTokenPricing` variant parsed from the provider record's
`config.cost`. Admission callers should bind the request payload to a `CostQuote` and reserve
`upper_bound()`, which uses `max_tokens` / `max_output_tokens` / `max_completion_tokens` /
`max_new_tokens` as the completion ceiling before Together returns actual usage. The exact
completion cost is therefore unknown until response usage is available, but the pre-spend gate can
still reserve a Decimal upper bound.

`TogetherProvider.infer()` is the direct inference helper. It adds the provider record's `config`
`model` (or accepts a payload-provided `model` override), posts the request body with header-only
auth, and maps the OpenAI-compatible response into `TogetherInferenceResult` with content, model,
finish reason, token usage, and raw response. `provision`, `status`, `reconcile`, and `teardown`
raise `NotImplementedError` because Together is not a leaseable resource provider.

### `workloads.py`
`WorkloadConfig` (Pydantic BaseModel): `name`, `capability`, `template_name`, `gpu_types: list[str]` (min_length=1), `gpu_count`, `container_disk_gb`, `min_vcpu`, `min_memory_gb`, `cloud_type`, `gpu_type_priority`, `data_center_priority`, `allowed_cuda_versions`, `ports`. **Invariant:** `gpu_types` is validated at construction via `validate_canonical_gpu_names()` — shorthand aliases like `"H100"` are rejected (`workloads.py:40–43`).

### `gpu.py`
Shorthand aliases are rejected rather than silently normalized.

```python
CANONICAL_GPU_NAMES: frozenset[str]  # 32 entries, e.g. "NVIDIA H100 80GB HBM3"
def is_canonical_gpu_name(gpu_name: str) -> bool
def canonical_gpu_name_suggestions(gpu_name: str) -> tuple[str, ...]
def validate_canonical_gpu_name(gpu_name: str) -> str
def validate_canonical_gpu_names(gpu_names: Iterable[str]) -> list[str]
class NonCanonicalGPUNameError(ValueError):
    gpu_name: str; suggestions: tuple[str, ...]
# aliases: validate_gpu_type, validate_gpu_types
```

Shorthand suggestions in `_SHORTHAND_GPU_SUGGESTIONS` (`gpu.py:53–73`) are diagnostic only — validation always raises.

### `registry.py`
Selects `containerRegistryAuthId` by image prefix (GHCR, GitLab, Docker Hub). Also provides async CRUD for RunPod container-registry credentials via `https://rest.runpod.io/v1`.

**Env-helper functions** (unchanged):

```python
GHCR_PREFIX = "ghcr.io"; GITLAB_REGISTRY_PREFIX = "registry.gitlab.com"; DOCKER_HUB_PREFIX = "docker.io"
LEGACY_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID"
GHCR_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID_GHCR"
GITLAB_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID_GITLAB"
DOCKER_HUB_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB"

def image_registry_prefix(image_ref: str | None) -> str | None
def registry_auth_env_names_for_image_ref(image_ref: str | None) -> tuple[str, ...]
def registry_auth_id_from_env(image_ref: str | None = None, *, environ: Mapping | None = None) -> str | None
```

GHCR and GitLab fall back to `LEGACY_REGISTRY_AUTH_ENV`. `image_ref=None` uses `LEGACY` → `GHCR` priority.

**CRUD surface** (`registry.py:38–229`):

```python
class RegistryAuthError(RuntimeError): ...

class RegistryAuthCreateInput(BaseModel):
    name: str; username: str; password: str

class ContainerRegistryAuth(BaseModel):
    # RunPod never returns username or password after creation
    id: str; name: str

async def create_container_registry_auth(name, username, password, *, timeout_s=60.0) -> ContainerRegistryAuth
    # POST /containerregistryauth — raises RegistryAuthError on 4xx/5xx

async def get_container_registry_auth(auth_id, *, timeout_s=60.0) -> ContainerRegistryAuth | None
    # GET /containerregistryauth/{auth_id} — returns None on 404

async def list_container_registry_auths(*, timeout_s=60.0) -> list[ContainerRegistryAuth]
    # GET /containerregistryauth — raises RegistryAuthError on 5xx

async def delete_container_registry_auth(auth_id, *, timeout_s=60.0) -> None
    # DELETE /containerregistryauth/{auth_id} — idempotent (404 swallowed)
```

Auth: `Bearer {RUNPOD_API_KEY}`, base `https://rest.runpod.io/v1` (configurable via `RUNPOD_REST_API_URL`).

### `pods.py`
Pod lifecycle via `rest.runpod.io`. Handles readiness probing, GPU/cloud fallback, volume-attach hang detection, cost gates.

**REST helper:** `_rest_request(method, path, *, json_body, params, timeout_s=60.0)` → uses `httpx.Client`, `Authorization: Bearer {RUNPOD_API_KEY}`, base from `RUNPOD_REST_API_URL` (default `https://rest.runpod.io/v1`). Raises `RunPodRestError` on 4xx/5xx (`pods.py:186–220`).

**Exceptions:** `RunPodError` (base), `NoCapacityError` (all GPU types exhausted), `ProviderFallbackRequested` (skip provider), `PodStartupTimeout`, `PodVolumeAttachTimeout(pod_id, attach_timeout_s)`, `ProviderAttachHangRecoveryRequested(pod_id, attach_timeout_s)`, `PodStartupFailed`, `RunPodRestError(method, path, status_code, body)`.

**Probe order:** `ssh_localhost` (placeholder → `not_configured`) then `runpod_proxy` (`https://{pod_id}-{port}.proxy.runpod.net`). 524 is distinguished from other errors (`pods.py:959–1076`).

**Create:** `create_pod_with_fallback_sync(name, template_id, image_name, workload: WorkloadConfig, env, cloud_type_override, network_volume_id, data_center_id, docker_entrypoint, docker_start_cmd, container_registry_auth_id, support_public_ip, max_cost_per_hr, max_pod_attempts, timeout_per_attempt_s=120.0, startup_timeout_s=600.0, startup_poll_s=15.0, volume_attach_timeout_s, pre_readiness_callback, wait_for_readiness)` — iterates GPU type priority then cloud type; on capacity error (matched via `PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS`) retries next combination; pre-readiness guards check GPU count/type and cost ceiling; raises `ProviderFallbackRequested` or propagates `PodVolumeAttachTimeout`/`PodStartupFailed` after deleting the pod (`pods.py:536–733`).

**Readiness wait:** `wait_for_pod_runtime_sync(pod_id, initial, timeout_s=600.0, poll_s=15.0, volume_attach_timeout_s)` — polls `GET /pods/{pod_id}` until `runtime` + `portMappings` + probe. Attaches readiness signals to `pod["readiness"]`. Raises `PodStartupTimeout`, `PodVolumeAttachTimeout`, or `PodStartupFailed` (`pods.py:1120–1201`).

**Getters:** `get_pods_sync() -> list[dict]`, `get_pod_sync(pod_id) -> dict | None` (falls back to SDK/GraphQL when REST lacks `runtime`/`portMappings`). Async variants delegate via `asyncio.to_thread`.

**Terminators:** `terminate_pod_sync(pod_id)` (idempotent: 404 swallowed), `terminate_all_with_tag(name_prefix="pitwall-") -> int`.

**Lifecycle:** `start_pod_sync(pod_id)` (POST `/pods/{id}/start`), `stop_pod_sync(pod_id)` (POST `/pods/{id}/stop`), `reset_pod_sync(pod_id)` (POST `/pods/{id}/reset`), `restart_pod_sync(pod_id)` (stop then start), `update_pod_sync(pod_id, env, ports, container_registry_auth_id)` (PATCH `/pods/{id}`). All have async variants via `asyncio.to_thread`. Update requires at least one field; raises `RunPodError` if called with no fields. Models: `UpdatePodRequest` (Pydantic BaseModel, extra-forbid), `UpdatePodResponse` (Pydantic BaseModel, extra-allow).

**Env vars:** `PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS` (default includes `"no longer any instances available"`, `"resourcesunavailable"`, `"insufficient"`), `PITWALL_VOLUME_ATTACH_TIMEOUT_S` (default 300.0s) (`pods.py:30–41`).

### `queue.py`
Async httpx wrapper for RunPod queue-based serverless (`api.runpod.ai/v2/{endpoint_id}/`). Auth: `Bearer {RUNPOD_API_KEY}`.

```python
class QueueJob(BaseModel): id, status, output, error, raw
class QueueHealth(BaseModel): jobs: dict[str, int], raw
class QueueCancelResult(BaseModel): cancelled, raw
class QueuePurgeResult(BaseModel): purged, raw
class RateLimitFailure(BaseModel): endpoint_id, path, retry_after_s, retry_after_header, status_code=429, occurred_at: dt.datetime

class QueueClient(api_key, timeout_s=600, retry_delays=(1.0,3.0,9.0), max_retry_after_s, sleep=asyncio.sleep, clock=_utc_now, transport, on_429)
    async runsync(endpoint_id, input, webhook=None, policy=None) -> QueueJob
    async run(endpoint_id, input, webhook=None, policy=None) -> QueueJob
    async status(endpoint_id, job_id) -> QueueJob
    async health(endpoint_id) -> QueueHealth
    async cancel(endpoint_id, job_id) -> QueueCancelResult
    async purge_queue(endpoint_id) -> QueuePurgeResult
```

Retries on 429 (bounded by `Retry-After` up to `max_retry_after_s`), 5xx, transient `HTTPError`. `on_429` callback receives structured `RateLimitFailure`. Does not retry 4xx (`queue.py:134–184`).

### `lb.py`
Async httpx wrapper for RunPod load-balancer (`{endpoint_id}.api.runpod.ai`). Auth: `Bearer {RUNPOD_API_KEY}`.

```python
class ProbeResult(BaseModel): healthy, status_code, error, latency_ms
class LBResponse(BaseModel): status_code, data, raw

class LBClient(api_key, timeout_s=120.0, retry_delays=(1.0,3.0,9.0), max_retry_after_s, sleep, clock, transport)
    async post(endpoint_id, path, json) -> LBResponse
    async get(endpoint_id, path) -> LBResponse
    async ping(endpoint_id) -> bool  # True on 200
    async probe(endpoint_id, path="/ping", timeout_s=5.0) -> ProbeResult
```

`probe()` sets `error="524"` for Cloudflare 524, `"timeout"` for timeout, and `"connection_error"` for network errors (`lb.py:217–225`).

### `serverless.py`
Async httpx wrapper for `/v1/chat/completions` on operator-configured RunPod Serverless endpoints. `base_url` must end with `/openai/v1`. Pitwall does not publish the endpoint worker image.

```python
class ServerlessResponse(BaseModel): content, model, input_tokens, output_tokens, finish_reason, duration_ms, raw

class ServerlessClient(base_url, api_key, model, timeout_s=600, retry_delays=(1.0,3.0,9.0), max_retry_after_s, sleep, clock, transport)
    async chat_completion(messages, max_tokens, temperature=0.0, extra=None) -> ServerlessResponse
```

Retry behavior identical to `QueueClient` (`serverless.py:118–163`).

Also exposes async CRUD for RunPod serverless endpoint management via the REST API (`https://rest.runpod.io/v1`):

```python
class EndpointScalingConfig(BaseModel):
    workers_min: int = 0        # min idle workers kept warm
    workers_max: int = 3         # max concurrent workers
    idle_timeout: int = 60      # seconds before idle worker stops
    gpu_type_id: str | None     # e.g. "NVIDIA L4"; None = auto
    flashboot: bool = False      # enable flashboot for faster cold-start
    def to_request_json() -> dict[str, Any]  # camelCase for RunPod API

class Endpoint(BaseModel):
    id: str; name: str; scaling: EndpointScalingConfig
    template_id: str | None; created_at: str | None; raw: dict[str, Any]

async def create_endpoint(name, template_id, *, gpu_ids=None, scaling=None, timeout_s=60.0) -> Endpoint
async def get_endpoint(endpoint_id, *, timeout_s=60.0) -> Endpoint
async def list_endpoints(*, name_prefix=None, timeout_s=60.0) -> list[Endpoint]
async def update_endpoint_scaling(endpoint_id, scaling: EndpointScalingConfig, *, timeout_s=60.0) -> Endpoint
async def delete_endpoint(endpoint_id, *, timeout_s=60.0) -> dict[str, Any]
```

All functions use `_rest_request_async` with `httpx.AsyncClient`. `endpoint_id` is normalized (strip whitespace/slashes, validated non-empty and no path separators). Raises `RunPodRestError` on HTTP 4xx/5xx. The `RunPodError` and `RunPodRestError` types are imported from `pods.py` (`serverless.py:182–454`).

### `serverless_lb.py`
Async httpx wrapper for BGE-M3 `/embed` on RunPod LB. Supports "Pitwall mode" via `PITWALL_EMBEDDING_VIA_PITWALL` feature flag.

```python
class EmbeddingResponse(BaseModel): dense, sparse, colbert, raw

class ServerlessLBClient(lb_base_url, api_key=None, timeout_s=330.0, retry_attempts=4, retry_backoff_s=2.0, transport)
    async embed(texts, return_dense=True, return_sparse=True, return_colbert=False) -> dict
```

When `PITWALL_EMBEDDING_VIA_PITWALL=true` (via `load_settings_from_env()`), routes through Pitwall `/v1/inference`. 30 MB payload limit (`MAX_REQUEST_BODY_BYTES`) raises `ValueError` if exceeded (`serverless_lb.py:85–98, 26`).

### `templates.py`
Creates RunPod templates once per image SHA, caches in PostgreSQL `pitwall.runpod_templates` keyed on `(name, image_sha)`. Also provides get/update/delete for managing existing templates and Hub (public marketplace) template discovery.

```python
TEMPLATE_NAME = "pitwall-cloud-worker"

class TemplateEnvVar(BaseModel): key, value
class Template(BaseModel): id, name, image_name, docker_args, container_disk_in_gb,
    volume_in_gb, volume_mount_path, ports, env, is_serverless, is_public, readme
class HubTemplate(BaseModel): id, name, image_name, description, github_url, docker_args,
    container_disk_in_gb, volume_in_gb, volume_mount_path, ports, env, is_serverless,
    display_name, template_description

def image_sha(image_ref: str) -> str
def template_suffix(image_ref: str) -> str      # sha256[:12]
def normalize_template_name(name: str) -> str
def template_display_name(template_name: str, image_ref: str) -> str

async def ensure_template(pool, image_ref, *, template_name=TEMPLATE_NAME,
                          registry_auth_id=None, container_disk_gb=50,
                          volume_mount_path="/workspace") -> str
async def get_template(template_id: str) -> Template
async def update_template(template_id, *, name=None, image_name=None, docker_args=None,
                          container_disk_in_gb=None, volume_in_gb=None, volume_mount_path=None,
                          ports=None, env=None, is_serverless=None, is_public=None,
                          readme=None) -> Template
async def delete_template(template_id: str) -> bool
async def list_hub_templates(*, limit=50, offset=0) -> list[HubTemplate]
async def get_hub_template(template_id: str) -> HubTemplate

def get_image_ref_from_env() -> str    # reads PITWALL_CLOUD_WORKER_IMAGE; raises RuntimeError if unset
def get_registry_auth_id_from_env(image_ref=None) -> str | None

class TemplateNotFoundError(RuntimeError)
class TemplateDeleteError(RuntimeError)
```

Cache lookup is async; create uses `asyncio.to_thread`. On duplicate-name error from RunPod, falls back to GraphQL template listing and resolves ID by display name (`templates.py:75–95, 196–224`). Get/update/delete and Hub operations use `_run_graphql()` for direct GraphQL queries/mutations (`templates.py:208–410`).

### `graphql.py`
Async `httpx.AsyncClient` wrapper for RunPod's GraphQL endpoint (`https://api.runpod.io/graphql`), used for surfaces that REST cannot provide: live GPU prices, spot bid floor reads, spot bid resume mutation, datacenter/GPU availability enumeration, and credit balance reads.

**Construction:** `RunpodGraphQLClient(api_key, graphql_url=RUNPOD_GRAPHQL_URL, timeout_s=60.0, transport=None)`; `RunpodGraphQLClient.from_settings(settings=None, ...)` reads `PitwallSettings.runpod_api_key` via `load_settings_from_env()`. Requests authenticate only with `Authorization: Bearer {api_key}` plus `Content-Type: application/json`. The API key is never placed in the GraphQL URL.

**Models:** All response objects are Pydantic v2 models with camelCase aliases enabled. Money/price fields are `Decimal`, and response JSON is decoded with `json.loads(..., parse_float=Decimal)` so GraphQL numeric text is preserved exactly.

```python
class RunpodGraphQLClient:
    async def gpu_types() -> list[RunpodGpuType]
    async def datacenters() -> list[RunpodDatacenter]
    async def get_bid_price(gpu_type_id, dc=None, *, data_center_id=None, secure_cloud=None, gpu_count=1) -> RunpodBidPrice
    async def set_bid_price(*, pod_id, bid_per_gpu: Decimal, gpu_count=1) -> RunpodBidResumeResult
    async def credits_balance() -> RunpodCreditsBalance
    async def aclose() -> None
```

**`gpu_types()` query:** calls `gpuTypes` and returns `RunpodGpuType` entries with `id`, `displayName`, `memoryInGb`, `secureCloud`, `communityCloud`, on-demand price fields (`securePrice`, `communityPrice`, reservation prices), spot price fields (`secureSpotPrice`, `communitySpotPrice`), `lowestPrice`, and `nodeGroupDatacenters`. This is the discovery source for downstream GPU catalog and pricing stories.

**`datacenters()` query:** calls `myself { datacenters { ... gpuAvailability { ... } } }` and returns `RunpodDatacenter` with `gpu_availability` entries keyed by `gpu_type_id`, `stock_status`, and `available`. This is the GraphQL-only replacement for guessing availability from failed pod creation.

**Spot bid seam:** `get_bid_price()` calls `gpuTypes(input: {id})` with `lowestPrice(input: {gpuCount, dataCenterId, secureCloud, globalNetwork: false})` and returns `RunpodBidPrice` (`minimum_bid_price`, `uninterruptable_price`, spot fields, stock status, available GPU counts). `set_bid_price()` uses the `podBidResume` mutation for interruptible pods; the bid is formatted from `Decimal` into a GraphQL numeric literal and is never converted through Python `float`.

**Billing seam:** `credits_balance()` calls `myself { clientBalance currentSpendPerHr spendLimit minBalance underBalance }` and returns `RunpodCreditsBalance`. This is the account-credit read used by billing/credits work; spend admission still uses Pitwall's local budget gate as the source of policy.

`pitwall.cost.billing_read` wraps this seam for FinOps reconciliation:
- `read_billing_snapshot(client)` — thin async wrapper returning `BillingSnapshot` (typed `Decimal` fields)
- `reconcile_with_budget(client, budget_gate)` — compares RunPod `clientBalance` against Pitwall's `monthly_budget - mtd_spend` and returns `BudgetReconciliation` with `variance_usd`

**Errors:** GraphQL error envelopes (`{"errors": [...]}`) raise `RunpodGraphQLError`, a `RunPodError` subclass with the original errors retained. Non-2xx HTTP responses raise `RunpodGraphQLHTTPError`; malformed envelopes raise `RunpodGraphQLResponseError`.

### `availability.py`
5-minute TTL in-process cache keyed by `(datacenter, gpu_name, cloud_type, gpu_count)`. Thread-safe via `threading.RLock`.

```python
@dataclass(frozen=True)
class AvailabilityKey: datacenter, gpu_name, cloud_type, gpu_count
@dataclass
class AvailabilityValue: available, checked_at

class AvailabilityCache(DEFAULT_TTL_S=300.0):
    def is_available(datacenter, gpu_name, cloud_type, gpu_count) -> bool | None
        # None=missing/expired; True=available; False=unavailable
    def set_available(datacenter, gpu_name, cloud_type, gpu_count, available) -> None
    def bulk_set_available(list[tuple]) -> None
    def invalidate() -> None; def sweep_expired() -> int

def get_global_availability_cache() -> AvailabilityCache
def reset_global_availability_cache() -> None  # testing only
```

### `discovery.py`
GPU-type + datacenter discovery service.  Normalizes the raw GraphQL `gpuTypes` and `myself.datacenters` responses into an immutable, replay-friendly catalog snapshot.

```python
@dataclass(frozen=True, slots=True)
class GpuCatalogEntry:
    gpu_type_id: str
    display_name: str | None
    manufacturer: str | None
    memory_in_gb: int | None
    cuda_cores: int | None
    secure_cloud: bool
    community_cloud: bool
    secure_price: Decimal | None
    community_price: Decimal | None
    secure_spot_price: Decimal | None
    community_spot_price: Decimal | None
    lowest_bid_price: Decimal | None
    uninterruptable_price: Decimal | None
    datacenter_ids: tuple[str, ...]
    available_gpu_counts: tuple[int, ...]
    stock_status: str | None
    max_gpu_count: int | None

@dataclass(frozen=True, slots=True)
class DatacenterCatalogEntry:
    datacenter_id: str
    name: str | None
    location: str | None
    global_network: bool
    storage_support: bool
    listed: bool
    compliance: tuple[str, ...]
    gpu_types: tuple[str, ...]
    gpu_availability: Mapping[str, bool]

@dataclass(frozen=True, slots=True)
class GpuDiscoverySnapshot:
    fetched_at: datetime
    gpus: tuple[GpuCatalogEntry, ...]
    datacenters: tuple[DatacenterCatalogEntry, ...]
    def gpu_by_id(gpu_type_id) -> GpuCatalogEntry | None
    def datacenter_by_id(datacenter_id) -> DatacenterCatalogEntry | None
    def to_availability_entries(gpu_count=1, cloud_type=None) -> list[tuple]
    def to_availability_snapshot(gpu_count=1, cloud_type=None) -> AvailabilitySnapshot

class GpuDiscoveryService(graphql_client, *, ttl_s=60.0):
    async def refresh() -> GpuDiscoverySnapshot
    async def get_snapshot() -> GpuDiscoverySnapshot
    def get_gpu(gpu_type_id) -> GpuCatalogEntry | None
    def get_datacenter(datacenter_id) -> DatacenterCatalogEntry | None
    def invalidate() -> None
    async def aclose() -> None
```

**TTL:** Default 60 seconds (`DEFAULT_DISCOVERY_TTL_S`).  Refresh is serialized behind an `asyncio.Lock` so concurrent callers share one GraphQL round-trip.

**Replay substrate:** `GpuDiscoverySnapshot.to_availability_entries()` and `to_availability_snapshot()` flatten the discovered catalog into the same `(datacenter, gpu_name, cloud_type, gpu_count, available)` tuples consumed by `PlanningContext.replay()`.  Callers may capture a snapshot, freeze it, and replay deterministic routing decisions against historical or hypothetical capacity without touching live GraphQL.

### `endpoints.py`
```python
def hibernate_endpoint(endpoint_id: str) -> dict[str, Any]
```
Normalizes `endpoint_id`, calls `PATCH /endpoints/{id}` with `{"workersMin": 0}` via `_rest_request`. Raises `RunPodError` if response is not a dict (`endpoints.py:10–31`).

### `mounts.py`
```python
POD_VOLUME_MOUNT_PATH = "/workspace"
SERVERLESS_VOLUME_MOUNT_PATH = "/runpod-volume"
POD_PROVIDER_TYPES = frozenset({ProviderType.POD_LEASE})
SERVERLESS_PROVIDER_TYPES = frozenset({ProviderType.SERVERLESS_QUEUE, ProviderType.SERVERLESS_LB, ProviderType.PUBLIC_ENDPOINT})
PROVIDER_TYPE_VOLUME_MOUNT_PATHS: Mapping[ProviderType, str]  # covers all 4 RunPod provider types

def provider_type_volume_mount_path(provider_type: ProviderType | str) -> str
mount_path_for_provider_type = provider_type_volume_mount_path  # alias

class NetworkVolume(BaseModel): id, name, size, data_center_id
class S3Object(BaseModel): key, size, last_modified

class NetworkVolumeClient(api_key, rest_base_url, s3_access_key, s3_secret_key, timeout_s=60.0, transport)
    async create(name, size_gb, dc) -> NetworkVolume
    async get(volume_id) -> NetworkVolume
    async list() -> list[NetworkVolume]
    async update(volume_id, size_gb) -> NetworkVolume
    async delete(volume_id) -> None          # idempotent on 404
    async list_objects(volume_id, dc, prefix="") -> list[S3Object]
    async put_object(volume_id, dc, key, body) -> None
    async get_object(volume_id, dc, key) -> bytes
    async delete_object(volume_id, dc, key) -> None
```
REST operations use `httpx.AsyncClient` against `rest.runpod.io/v1` (configurable). S3 operations use `boto3` wrapped in `asyncio.to_thread` against `s3api-{dc}.runpod.io` with path-style addressing and SigV4 signing. `NetworkVolume.data_center_id` accepts the RunPod camelCase alias `dataCenterId` at parse time (`populate_by_name=True`). `ValueError` for unknown provider types in `provider_type_volume_mount_path`. `ProviderType` from `pitwall.core.enums`.

---

## 3. RunPod Surfaces

| Surface | Module | Auth | Base URL |
|---|---|---|---|
| Pod CRUD | `pods.py` | `Bearer {RUNPOD_API_KEY}` | `https://rest.runpod.io/v1` (configurable) |
| GPU market, datacenters, spot bids, credits | `graphql.py` | `api_key` query + `Bearer {RUNPOD_API_KEY}` | `https://api.runpod.io/graphql` |
| Container registry auth CRUD | `registry.py` | `Bearer {RUNPOD_API_KEY}` | `https://rest.runpod.io/v1` (configurable) |
| Network volume CRUD + S3 | `mounts.py` | REST: `Bearer {RUNPOD_API_KEY}`; S3: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | REST: `https://rest.runpod.io/v1`; S3: `https://s3api-{dc}.runpod.io` |
| GPU catalog + datacenter discovery | `discovery.py` | Delegates to `graphql.py` | n/a (in-process) |
| GPU market, datacenters, spot bids, credits | `graphql.py` | `Bearer {RUNPOD_API_KEY}` | `https://api.runpod.io/graphql` |
| Template management | `templates.py` | SDK reads `runpod.api_key` | RunPod GraphQL |
| Serverless queue | `queue.py` | `Bearer {RUNPOD_API_KEY}` | `https://api.runpod.ai/v2` |
| Serverless load-balancer | `lb.py` | `Bearer {RUNPOD_API_KEY}` | `https://{endpoint_id}.api.runpod.ai` |
| Serverless OpenAI-compatible | `serverless.py` | `Bearer {api_key}` | caller-provided, must end with `/openai/v1` |
| Serverless LB embedding | `serverless_lb.py` | optional `Bearer {api_key}` | `{lb_base_url}/embed` |
| Serverless endpoint admin (CRUD) | `serverless.py` | `Bearer {RUNPOD_API_KEY}` | `https://rest.runpod.io/v1` (configurable) |
| Endpoint admin (hibernate) | `endpoints.py` | same as pods | `https://rest.runpod.io/v1` |

---

## 4. Public Interfaces

Key callable symbols (RunPod client symbols are re-exported from `pitwall.runpod_client`;
provider plugin symbols are re-exported from `pitwall.providers`):

- **Config:** `WorkloadConfig`, `CANONICAL_GPU_NAMES`
- **Provider plugins:** `Provider`, `ProviderRegistry`, `ProviderOperationContext`, `ProvisionRequest`, `ProvisionResult`, `StatusRequest`, `StatusResult`, `ReconcileRequest`, `ReconcileResult`, `TeardownRequest`, `TeardownResult`, `ResourceStatus`, `RunPodProvider`, `RunPodCredentials`, `create_default_registry`, `get_default_registry`; registry errors: `DuplicateProviderError`, `ProviderNotRegisteredError`, `CredentialValidationError`
- **GPU validation:** `validate_canonical_gpu_name`, `validate_canonical_gpu_names`, `is_canonical_gpu_name`, `canonical_gpu_name_suggestions`, `NonCanonicalGPUNameError`
- **Registry:** `registry_auth_id_from_env`, `image_registry_prefix`, `registry_auth_env_names_for_image_ref`; **CRUD:** `create_container_registry_auth`, `get_container_registry_auth`, `list_container_registry_auths`, `delete_container_registry_auth`, `ContainerRegistryAuth`, `RegistryAuthCreateInput`, `RegistryAuthError`
- **Pods:** `create_pod_with_fallback` (+ sync variant), `wait_for_pod_runtime` (+ sync), `get_pods` (+ sync), `get_pod` (+ sync), `terminate_pod` (+ sync), `terminate_all_with_tag`, `get_pods_by_tag_prefix`; exceptions: `RunPodError`, `NoCapacityError`, `PodStartupTimeout`, `PodVolumeAttachTimeout`, `ProviderAttachHangRecoveryRequested`, `PodStartupFailed`, `RunPodRestError`; `PodProbeResult`
- **Registry:** `registry_auth_id_from_env`, `image_registry_prefix`, `registry_auth_env_names_for_image_ref`
- **Pods:** `create_pod_with_fallback` (+ sync variant), `wait_for_pod_runtime` (+ sync), `get_pods` (+ sync), `get_pod` (+ sync), `terminate_pod` (+ sync), `terminate_all_with_tag`, `get_pods_by_tag_prefix`, `start_pod` (+ sync), `stop_pod` (+ sync), `reset_pod` (+ sync), `restart_pod` (+ sync), `update_pod` (+ sync); exceptions: `RunPodError`, `NoCapacityError`, `PodStartupTimeout`, `PodVolumeAttachTimeout`, `ProviderAttachHangRecoveryRequested`, `PodStartupFailed`, `RunPodRestError`; `PodProbeResult`, `UpdatePodRequest`, `UpdatePodResponse`
- **Queue:** `QueueClient`, `QueueJob`, `QueueHealth`, `QueueCancelResult`, `QueuePurgeResult`, `RateLimitFailure`, `RUNPOD_API_BASE`, `queue_url`
- **LB:** `LBClient`, `ProbeResult`, `LBResponse`, `lb_endpoint_url`
- **Serverless:** `ServerlessClient`, `ServerlessResponse`; endpoint CRUD: `create_endpoint`, `get_endpoint`, `list_endpoints`, `update_endpoint_scaling`, `delete_endpoint`, `Endpoint`, `EndpointScalingConfig`
- **Embedding LB:** `ServerlessLBClient`, `EmbeddingResponse`
- **GraphQL market/billing:** `RunpodGraphQLClient` (`RunPodGraphQLClient` alias), `RunpodGpuType`, `RunpodDatacenter`, `RunpodGpuAvailability`, `RunpodLowestPrice`, `RunpodBidPrice`, `RunpodBidResumeResult`, `RunpodCreditsBalance`, `RunpodGraphQLError`, `RunpodGraphQLHTTPError`, `RunpodGraphQLResponseError`, `RUNPOD_GRAPHQL_URL`
- **Discovery:** `GpuDiscoveryService`, `GpuDiscoverySnapshot`, `GpuCatalogEntry`, `DatacenterCatalogEntry`, `DEFAULT_DISCOVERY_TTL_S`
- **Billing read / reconciliation:** `BillingSnapshot`, `BudgetReconciliation`, `BudgetGateLike`, `read_billing_snapshot`, `reconcile_with_budget` (from `pitwall.cost.billing_read`)
- **Templates:** `ensure_template`, `get_image_ref_from_env`, `get_registry_auth_id_from_env`, `image_sha`, `normalize_template_name`, `template_display_name`, `template_suffix`, `TEMPLATE_NAME`
- **Templates:** `ensure_template`, `get_template`, `update_template`, `delete_template`, `list_hub_templates`, `get_hub_template`, `get_image_ref_from_env`, `get_registry_auth_id_from_env`, `image_sha`, `normalize_template_name`, `template_display_name`, `template_suffix`, `TEMPLATE_NAME`, `Template`, `HubTemplate`, `TemplateEnvVar`, `TemplateNotFoundError`, `TemplateDeleteError`
- **Endpoints:** `hibernate_endpoint`
- **Availability:** `AvailabilityCache`, `get_global_availability_cache`, `reset_global_availability_cache`
- **Mounts:** `PROVIDER_TYPE_VOLUME_MOUNT_PATHS`, `provider_type_volume_mount_path`, `POD_VOLUME_MOUNT_PATH`, `SERVERLESS_VOLUME_MOUNT_PATH`
- **Network volumes:** `NetworkVolumeClient`, `NetworkVolume`, `S3Object`; methods: `create`, `get`, `list`, `update`, `delete`, `list_objects`, `put_object`, `get_object`, `delete_object`

---

## 5. Configuration

| Env var | Module(s) | Default | Description |
|---|---|---|---|
| `RUNPOD_API_KEY` | all | *(required)* | Bearer token for all RunPod API calls |
| `RUNPOD_REST_API_URL` | pods, endpoints, registry | `https://rest.runpod.io/v1` | Base URL for Pod REST, endpoint admin, and registry auth CRUD |
| `PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS` | pods | `"no longer any instances available"`, `"resourcesunavailable"`, `"insufficient"`, etc. | Capacity error substring list |
| `PITWALL_VOLUME_ATTACH_TIMEOUT_S` | pods | `300.0` | Volume attach hang timeout (seconds) |
| `RUNPOD_REGISTRY_AUTH_ID` | registry | *(none)* | Legacy GHCR-compatible registry auth ID |
| `RUNPOD_REGISTRY_AUTH_ID_GHCR` | registry | *(none)* | GHCR registry auth ID |
| `RUNPOD_REGISTRY_AUTH_ID_GITLAB` | registry | *(none)* | GitLab registry auth ID |
| `RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB` | registry | *(none)* | Docker Hub registry auth ID |
| `PITWALL_CLOUD_WORKER_IMAGE` | templates | *(required)* | Image ref for cloud worker template |
| `PITWALL_EMBEDDING_VIA_PITWALL` | serverless_lb | *(none)* | Route embeddings through Pitwall instead of direct |
| `RUNPOD_S3_ACCESS_KEY` | mounts (S3) | *(falls back to `AWS_ACCESS_KEY_ID`)* | S3 API key access key for network volume file access |
| `RUNPOD_S3_SECRET_KEY` | mounts (S3) | *(falls back to `AWS_SECRET_ACCESS_KEY`)* | S3 API key secret for network volume file access |
| `PITWALL_BASE_URL` | serverless_lb | *(none)* | Pitwall base URL for embedding routing |

---

## 6. Failure Modes & Error Types

**Pod lifecycle:** `RunPodError` (base, treated as 5xx); `NoCapacityError` (all GPU/cloud combos exhausted → triggers provider fallback); `ProviderFallbackRequested` (pre-readiness cost/gpu gate → skip provider); `PodStartupTimeout` (never reached runtime within `startup_timeout_s`); `PodVolumeAttachTimeout` (zero-uptime volume hang → pod deleted, `ProviderAttachHangRecoveryRequested` raised); `PodStartupFailed` (terminal failed state reached); `RunPodRestError` (HTTP 4xx/5xx with method/path/status/body).

**GraphQL market/billing:** `RunpodGraphQLError` for `errors` envelopes, preserving the original error objects; `RunpodGraphQLHTTPError` for non-2xx endpoint responses; `RunpodGraphQLResponseError` for invalid JSON or missing `data` shapes. All are `RunPodError` subclasses so service code can route them with existing RunPod failure handling.
**Container registry auth CRUD:** `RegistryAuthError` (base); `create_container_registry_auth` raises on HTTP 4xx/5xx; `get_container_registry_auth` returns `None` on 404; `delete_container_registry_auth` is idempotent (404 swallowed); `list_container_registry_auths` raises on HTTP 5xx.

**Serverless queue:** `httpx.HTTPStatusError` (429 → `on_429` callback + retry; 5xx → retry with backoff; 4xx → raised immediately).

**GPU validation:** `NonCanonicalGPUNameError` raised at `WorkloadConfig` construction time; blocks launch.

**Template errors:** `RuntimeError` — `PITWALL_CLOUD_WORKER_IMAGE` env var missing or RunPod returns unexpected `create_template` response shape. `TemplateNotFoundError` — template ID does not exist (get/update/delete). `TemplateDeleteError` — template could not be deleted.

**Network volumes:** `RunPodRestError` on REST 4xx/5xx (same shape as pods: method/path/status/body). `RunPodError` when boto3 is missing for S3 operations. Non-dict/list REST responses raise `RunPodError` with the offending type name. Delete is idempotent: 404 is swallowed (`mounts.py:220–230`).

**Capacity detection:** `is_capacity_error(exc)` matches exception body against `PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS`. Matched errors trigger the next GPU/cloud fallback. Unmatched non-2xx responses are logged as warnings and re-raised immediately (`pods.py:325–350`).

---

## 7. Testing

| File | Coverage |
|---|---|
| `tests/runpod_client/test_pods.py` | Pod create, readiness wait, terminate, capacity substring matching, GPU/cloud fallback, probe ordering; start/stop/reset/restart/update lifecycle (happy path, error envelope, missing fields) |
| `tests/runpod_client/test_queue.py` | QueueClient (runsync, run, status, health, cancel, purge-queue), retry on 429/5xx, `on_429` callback |
| `tests/runpod_client/test_lb.py` | LBClient (post, get, ping, probe), 524 distinction, `ProbeResult` structure |
| `tests/runpod_client/test_serverless.py` | ServerlessClient chat_completion, retry on 429/5xx |
| `tests/runpod_client/test_serverless_endpoints.py` | `EndpointScalingConfig` validation, `to_request_json()` round-trip, `create_endpoint`, `get_endpoint`, `list_endpoints`, `update_endpoint_scaling`, `delete_endpoint` (happy path, REST errors, edge cases) |
| `tests/runpod_client/test_availability.py` | AvailabilityCache TTL, thread-safety, `bulk_set_available`, `sweep_expired` |
| `tests/runpod_client/test_registry.py` | `image_registry_prefix`, `registry_auth_env_names_for_image_ref`, `registry_auth_id_from_env`; CRUD: `create_container_registry_auth` (happy path, error envelope, REST URL, missing key), `get_container_registry_auth` (found, 404→None, 5xx, missing key), `list_container_registry_auths` (non-empty, empty, unexpected shape, 5xx, missing key), `delete_container_registry_auth` (happy path, idempotent 404, 5xx, missing key) |
| `tests/runpod_client/test_templates.py` | `ensure_template` (cache hit, miss+create, duplicate name handling), `image_sha`, `template_display_name` |
| `tests/runpod_client/test_registry.py` | `image_registry_prefix`, `registry_auth_env_names_for_image_ref`, `registry_auth_id_from_env`, env fallback priority |
| `tests/runpod_client/test_templates.py` | `ensure_template` (cache hit, miss+create, duplicate name handling), `get_template`, `update_template`, `delete_template`, `list_hub_templates`, `get_hub_template`, `image_sha`, `template_display_name`, `Template`/`HubTemplate` model validation |
| `tests/runpod_client/test_endpoints.py` | `hibernate_endpoint` happy path, REST error propagation |
| `tests/runpod_client/test_graphql.py` | GraphQL GPU types, datacenters, spot bid read/set, credits balance, error envelopes, Decimal fidelity, settings auth plumbing |
| `tests/runpod_client/test_mounts.py` | `NetworkVolumeClient` REST CRUD (create/get/list/update/delete), error-envelope handling, idempotent delete, S3 operations (list/put/get/delete objects), credential resolution, transport injection |
| `tests/runpod_client/test_discovery.py` | `GpuDiscoveryService` refresh, TTL, cache hit/miss, `GpuCatalogEntry`/`DatacenterCatalogEntry` fields, `to_availability_snapshot`, error propagation, concurrency |
| `tests/property/test_discovery_properties.py` | Determinism, entry-shape invariants, `_normalize_gpu`/`_normalize_datacenter` round-trips |
| `tests/providers/test_registry.py` | Provider protocol shape, registry registration/lookup, safe credential-schema validation, default RunPod registration, tagged pricing delegation |
| `tests/providers/test_runpod_adapter.py` | RunPod provider adapter delegation to existing launch/teardown/status/reconcile services |
| `tests/property/test_provider_registry_properties.py` | Credential URL userinfo rejection across generated secret strings |
| `tests/test_runpod_gpu_validator.py` | GPU canonicalization, WorkloadConfig validation, shorthand rejection with suggestions |
| `tests/test_runpod_mount_paths.py` | Mount path constants, provider-type mapping, enum/string accept |
| `tests/db/test_runpod_templates_migration.py` | PostgreSQL `runpod_templates` schema, unique constraint on `(name, image_sha)`, defaults |
| `tests/chaos/test_volume_attach_hang.py` | PodVolumeAttachTimeout behavior, zero-uptime detection |
| `tests/chaos/test_idempotent_terminate.py` | terminate_pod_sync idempotency on 404 |
| `tests/chaos/test_serverless_5xx.py` | ServerlessClient retry on 5xx |
| `tests/chaos/test_serverless_429.py` | ServerlessClient retry on 429 |
| `tests/leases/test_launch.py` | Pods + templates in lease launch flow |
| `tests/embedding/test_client_auth.py` | ServerlessLBClient embed flow, Pitwall mode, 30 MB limit |
| `tests/api/test_e2e_lease_lifecycle.py` | Lease create → get_pods → terminate end-to-end |
| `tests/cost/test_billing_read.py` | `read_billing_snapshot`, `reconcile_with_budget`, fake GraphQL transport, Decimal fidelity, error propagation, budget-gate protocol |
| `tests/fakes/runpod.py` | Shared fakes: `RunPodRestFake`, `RunPodServerlessFake`, `RunPodQueueFake`, `RunPodLBFake`, `RunPodTemplateFake`, `RunPodBillingFake` |

---

## 8. Dependencies

**Internal imports:**

| Module | From | Used for |
|---|---|---|
| `pods.py` | `pitwall.runpod_client.registry` | `registry_auth_id_from_env` |
| `pods.py` | `pitwall.runpod_client.workloads` | `WorkloadConfig` |
| `graphql.py` | `pitwall.config` | `PitwallSettings`, `load_settings_from_env` |
| `graphql.py` | `pitwall.runpod_client.pods` | `RunPodError` base class |
| `providers/runpod.py` | `pitwall.api.leases.launch` | Existing RunPod pod-lease provision flow |
| `providers/runpod.py` | `pitwall.api.leases.teardown` | Existing RunPod single-lease teardown flow |
| `providers/runpod.py` | `pitwall.cost.estimator` | Tagged pricing union parsing |
| `providers/runpod.py` | `pitwall.runpod_client.pods` | Pod status and reconcile reads |
| `templates.py` | `pitwall.runpod_client.pods` | `_sdk()` lazy SDK import |
| `templates.py` | `pitwall.runpod_client.registry` | `registry_auth_id_from_env` |
| `workloads.py` | `pitwall.runpod_client.gpu` | `validate_canonical_gpu_names` |
| `mounts.py` | `pitwall.core.enums` | `ProviderType` enum |
| `mounts.py` | `pitwall.runpod_client.pods` | `RunPodError`, `RunPodRestError` |
| `lb.py`, `queue.py`, `serverless.py` | `pitwall.rate_limits.retry_after` | `DEFAULT_MAX_RETRY_AFTER_DELAY_S`, `parse_retry_after` |
| `serverless.py` | `pitwall.runpod_client.pods` | `RunPodError`, `RunPodRestError` |
| `serverless_lb.py` | `pitwall.config` | `load_settings_from_env` |
| `endpoints.py` | `pitwall.runpod_client.pods` | `RunPodError`, `_rest_request` |
| `discovery.py` | `pitwall.runpod_client.graphql` | `RunpodGraphQLClient`, response models |
| `discovery.py` | `pitwall.routing.context` | `AvailabilitySnapshot`, `AvailabilityEntryInput` |
| `__init__.py` | `pitwall.core.enums` | `ProviderType` |

**External:**

| Library | Used in | Purpose |
|---|---|---|
| `httpx` | pods, graphql, queue, lb, serverless, serverless_lb | HTTP client (sync in pods, async elsewhere) |
| `pydantic` | workloads, graphql, queue, lb, serverless, serverless_lb | `BaseModel` response envelopes |
| `runpod` (SDK) | pods (`_sdk()`), templates | Template create + GraphQL listing; lazy import |
| `boto3` | mounts (S3) | S3-compatible file access for network volumes; lazy import |
| `asyncpg` | templates | PostgreSQL pool for template cache |
| `asyncio` | pods, queue, lb, serverless, templates | `asyncio.to_thread` for sync wrappers; `asyncio.sleep` |
| `threading` | availability | `RLock` for thread-safe cache |
| `datetime` | queue, lb, serverless | `_utc_now` clock for `RateLimitFailure` timestamps |
| `time` | pods, availability, lb | `monotonic` for TTL and latency measurement |
