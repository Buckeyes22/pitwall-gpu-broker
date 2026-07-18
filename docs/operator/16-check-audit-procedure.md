# 16-Check Audit Procedure — Operator Runbook

Runbook for executing the 16-check RunPod audit against a live Pitwall deployment.
The audit is a pre-spend gate: run it before any non-trivial pod spend and after
any change to RunPod-related configuration.

Spec references: §4.3 (acceptance criteria), §15.4 (capability audit endpoint), §B.1
Source: `src/pitwall/audit/sixteen_check.py`

---

## Pre-flight

1. Pitwall API is reachable and admin secret is set: `echo $PITWALL_ADMIN_SECRET`
2. `python -m pytest --version` (for local test execution)
3. `curl` or `http` CLI available (for capability audit endpoint)
4. Worktree is at `/path/to/pitwall-worktrees/worker-2`

---

## Execution Modes

### Mode 1 — CI (automated harness)

Runs the full 16-check suite against a `RuntimeAuditConfig` built from live
environment / DB. Exits 0 only when all 16 pass.

```bash
cd /path/to/pitwall-worktrees/worker-2
python -m pitwall.audit.sixteen_check --strict
```

Expected output: `Pitwall 16-check audit: 16/16 passed`

### Mode 2 — Local test execution (hermetic, no live RunPod calls)

Runs the full test suite with synthetic configs via `respx`-mocked RunPod surface.

```bash
cd /path/to/pitwall-worktrees/worker-2
python -m pytest tests/test_audit_sixteen_check.py -v
```

### Mode 3 — Capability audit endpoint

Run the 16-check audit as part of the full capability pre-spend audit via the REST
endpoint. Substitute `<name>` with the target capability, e.g. `embedding.bge-m3`.

```bash
curl -s -X POST \
  -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}" \
  https://pitwall.example.com/v1/admin/audit-capability/<name>
```

Expected: HTTP 200 with `ready_to_invoke: true` and `"16_check_runpod_audit"` entry
with `"pass": true` and a `checked_at` timestamp.

---

## The 16 Checks

### Check 1 — GPU IDs are canonical RunPod names (L1)

**What it verifies:** Every GPU type ID in workloads and provider config uses the
canonical full name as returned by `runpod.get_gpus()`. Abbreviations like `"H100"`
or `"B200"` silently fail to match capacity.

**Operator action:** Search workloads and provider configs for GPU type strings.
Verify they match `NVIDIA H100 80GB HBM3`, `NVIDIA L4`, `NVIDIA B200`, etc.

**Pitwall surface:** `pitwall.runpod_client.gpu.CANONICAL_GPU_NAMES` (set of valid names).
`check_01_gpu_ids_canonical` in `src/pitwall/audit/sixteen_check.py:187`.

**Landmine:** L1 — GPU IDs must be canonical full names. Non-canonical names silently
fail to find capacity.

---

### Check 2 — `cloud_type=ALL` is never combined with `networkVolumeId` (L2)

**What it verifies:** When a network volume is attached, `cloud_type` is forced to
`SECURE`. RunPod network volumes are Secure Cloud only; attempting COMMUNITY wastes
50% of fallback attempts.

**Operator action:** Inspect pod-launch params. If `networkVolumeId` is set, confirm
`cloud_type` is `SECURE` (or `ALL` is rewritten by the launcher before the API call).

**Pitwall surface:** `src/pitwall/runpod_client/pods.py:_cloud_types_for_rest()` rewrites
`ALL` to `["SECURE"]` when a `network_volume_id` is present. `check_02_cloud_type_volume`
in `src/pitwall/audit/sixteen_check.py:200`.

**Landmine:** L2 — `cloud_type=ALL` with `networkVolumeId` wastes 50% of fallback
attempts. RunPod requires `SECURE`.

---

### Check 3 — Pod readiness verified via `runtime != null` (L3)

**What it verifies:** Readiness is gated on `runtime != null`, not just
`desiredStatus == "RUNNING"`. A pod can be desired=RUNNING before the runtime
container exists.

**Operator action:** Inspect readiness probe configuration. Verify it reads the
`runtime` field from `pod_get()` responses before marking a pod ready.

**Pitwall surface:** `src/pitwall/runpod_client/pods.py:_pod_has_runtime()` checks
`runtime is not None`. `check_03_readiness_runtime` in
`src/pitwall/audit/sixteen_check.py:219`.

**Landmine:** L3 — `desiredStatus=RUNNING` is a lie if `runtime=null`. Pod-created
≠ runtime-existing.

---

### Check 4 — Cost-cap check fires before readiness wait (L4)

**What it verifies:** The pod cost-cap is evaluated immediately after pod creation,
before entering the readiness wait loop. A wrong-SKU substitution must be killed
before billing starts.

**Operator action:** Inspect `create_pod_with_fallback_sync` source. Confirm
`_gate_pod_cost_before_readiness` is called before `wait_for_pod_runtime_sync`.

**Pitwall surface:** `src/pitwall/runpod_client/pods.py:create_pod_with_fallback_sync`
calls cost guard before readiness wait. `check_04_cost_cap_before_readiness` in
`src/pitwall/audit/sixteen_check.py:246`.

**Landmine:** L4 — Proxy-readiness hits Cloudflare's 100s timeout returning 524.
Cost cap must fire first to avoid billing a misconfigured pod.

---

### Check 5 — `executionTimeout` respected with explicit max

**What it verifies:** Every pod launch has an explicit `executionTimeout` set and
an `executionTimeoutMax` that bounds it. Timeouts must be positive integers.

**Operator action:** Inspect timeout configuration in workload specs. Verify
`executionTimeout` and `executionTimeoutMax` are both present and `timeout <= max`.

**Pitwall surface:** `check_05_execution_timeout` in `src/pitwall/audit/sixteen_check.py:279`.

---

### Check 6 — `ttl >= executionTimeout + expected_queue_time`

**What it verifies:** The pod TTL is long enough to cover the execution timeout plus
the expected queue wait time before RunPod auto-expires the pod.

**Operator action:** Inspect timeout config. Verify `ttl >= executionTimeout + expected_queue_time`.

**Pitwall surface:** `check_06_ttl_ge_timeout_plus_queue` in
`src/pitwall/audit/sixteen_check.py:295`.

---

### Check 7 — Webhook receiver is idempotent and fast-200 (L8)

**What it verifies:** The RunPod webhook endpoint returns 200 within 50ms and handles
duplicate deliveries idempotently (deduplicated by `(runpod_job_id, attempt)`).

**Operator action:** Send a test POST to the webhook endpoint. Confirm response is
200 and latency < 50ms. Retry the same payload and confirm no duplicate processing.

**Pitwall surface:** `src/pitwall/webhook_receiver.py` exposes `POST /webhooks/runpod`
and `POST /runpod`. `check_07_webhook_idempotent_fast200` in
`src/pitwall/audit/sixteen_check.py:308`.

**Landmine:** L8 — RunPod webhook delivery is best-effort (2 retries, 10s delay).
Receiver must be idempotent and fast-200.

---

### Check 8 — Result retention windows respected (sync 1min, async 30min)

**What it verifies:** Sync results are persisted to Pitwall storage within 30s so they
survive RunPod's 60s sync retention. Async results are persisted within 300s to survive
RunPod's 1800s async retention.

**Operator action:** Inspect retention configuration. Verify `sync_retention_s >= 60`,
`async_retention_s >= 1800`, `persist_before_expiry: true`, and
`sync_persist_deadline_s < 60`, `async_persist_deadline_s < 1800`.

**Pitwall surface:** `check_08_retention_windows` in
`src/pitwall/audit/sixteen_check.py:326`. Constants: `SYNC_RESULT_RETENTION_S = 60`,
`ASYNC_RESULT_RETENTION_S = 1800`.

**Landmine:** L8 — RunPod result expiry is absolute. Results must be pulled to
Pitwall storage before expiry.

---

### Check 9 — Network-volume DC pin enforced (`dataCenterIds=[<one>]`)

**What it verifies:** When a network volume is attached, `dataCenterIds` is pinned to
exactly one datacenter. Cross-DC routing with volumes is unsupported by RunPod.

**Operator action:** Inspect volume config. If `networkVolumeId` is set, confirm
`dataCenterIds` has exactly one entry.

**Pitwall surface:** `check_09_dc_pin` in `src/pitwall/audit/sixteen_check.py:365`.

**Landmine:** L7 — Network-volume attach can hang globally with `uptimeSeconds=0`.
Single-DC pinning reduces blast radius.

---

### Check 10 — SSH-first probe pattern available for pod-mode readiness (L4)

**What it verifies:** Pod-mode readiness prefers an SSH-first localhost probe over
the RunPod proxy. SSH-first avoids Cloudflare 524 timeouts and distinguishes
"image still pulling" from "service not ready."

**Operator action:** Inspect probe config. Confirm `ssh_first: true` and that
`ssh_localhost` is in `probe_methods` and is the `primary_probe`.

**Pitwall surface:** `src/pitwall/runpod_client/pods.py:POD_READINESS_PROBE_ORDER`
starts with `SSH_LOCALHOST_PROBE_METHOD`. `check_10_ssh_first_probe` in
`src/pitwall/audit/sixteen_check.py:377`.

**Landmine:** L4 — Proxy probe hits Cloudflare 100s timeout (524). SSH-first
localhost probe is faster and more diagnostic.

---

### Check 11 — Image-pull timeout enforced (>= pod startup_timeout)

**What it verifies:** `image_pull_timeout_s` is set and is >= `startup_timeout_s`.
An image pull that outlasts the startup timeout would burn budget with no diagnostic path.

**Operator action:** Inspect image config. Confirm `image_pull_timeout_s >= startup_timeout_s`.

**Pitwall surface:** `check_11_image_pull_timeout` in `src/pitwall/audit/sixteen_check.py:395`.

---

### Check 12 — Container disk explicitly sized per workload (80GB vLLM, 40GB embed, 20GB slim)

**What it verifies:** Each workload type has an explicit `container_disk_gb` configured
that covers the unpacked image size plus workspace headroom.

**Operator action:** Inspect disk config. Verify `per_workload` has entries for
`vllm: 80`, `embed: 40`, `slim: 20` (or larger).

**Pitwall surface:** `REQUIRED_DISK_GB_BY_WORKLOAD` dict in `src/pitwall/audit/sixteen_check.py:37`.
`check_12_disk_sized` at line 409.

---

### Check 13 — Template create + cache pattern (no template-recreate on every launch)

**What it verifies:** Template creation is cached. On cache hit, the cached template
ID is reused. On cache miss, the template is created and inserted into the cache.

**Operator action:** Inspect `ensure_template` source. Confirm lookup happens before
create and created templates are inserted into the cache.

**Pitwall surface:** `src/pitwall/runpod_client/templates.py:ensure_template` does
cache lookup before create and inserts on miss. `check_13_template_cache` in
`src/pitwall/audit/sixteen_check.py:426`.

**Landmine:** L13 — Dep stacks need monkey-patches. Cached templates with pinned deps
avoid rebuild churn.

---

### Check 14 — Registry-auth-id selected per image-ref prefix (GHCR vs. GitLab Registry vs. Docker Hub)

**What it verifies:** Image references are mapped to the correct `container_registry_auth_id`
based on their prefix (`ghcr.io/`, `registry.gitlab.com/`, `docker.io/`). No image
pulls with a wrong or missing auth.

**Operator action:** Inspect registry config `prefix_to_auth_id` mapping. Confirm all
three prefixes are covered: GHCR, GitLab Registry, Docker Hub.

**Pitwall surface:** `src/pitwall/runpod_client/registry.py:registry_auth_id_from_env()`
selects auth ID by image prefix. `check_14_registry_auth` in
`src/pitwall/audit/sixteen_check.py:445`.

**Landmine:** L12 — the legacy Hugging Face CLI invocation is deprecated.
Registry auth selection must use the correct env var per prefix.

---

### Check 15 — `terminate_*` calls are idempotent (404 = success)

**What it verifies:** `terminate_pod_sync` treats a 404 response as success. Terminating
an already-gone pod must not raise an error.

**Operator action:** Call `terminate_pod_sync` with a known-nonexistent pod ID.
Confirm it returns without raising.

**Pitwall surface:** `src/pitwall/runpod_client/pods.py:terminate_pod_sync` catches
`RunPodRestError` with 404 and returns without raising. `check_15_terminate_idempotent`
in `src/pitwall/audit/sixteen_check.py:491`.

**Landmine:** L15 — `terminate-pod` vs `emergency-kill` confusion. Idempotent terminate
prevents spurious failures in cleanup scripts.

---

### Check 16 — Kill switch is atomic, 3-step, <30s

**What it verifies:** The kill switch executes exactly the three ordered steps:
`list_pods` → `terminate_all` → `verify`. Total budget must be under 30 seconds.

**Operator action:** Inspect kill switch configuration. Confirm steps are exactly
`("list_pods", "terminate_all", "verify")` and `budget_s < 30`.

**Pitwall surface:** `check_16_kill_switch_atomic` in `src/pitwall/audit/sixteen_check.py:520`.
`KILL_SWITCH_STEPS = ("list_pods", "terminate_all", "verify")` at line 47.

**Landmine:** L16 — "One concern per paid launch." The kill switch is the emergency
brake; it must be fast and unambiguous.

---

## Failure Response

If any check fails:

1. **Do not proceed with pod spend.** The check failure is a pre-spend gate.
2. Note the check number and the failure message.
3. Remediate the underlying configuration or code issue.
4. Re-run the audit in Mode 1 (CI) or Mode 2 (local test) to confirm the fix.
5. Once all 16 pass, proceed with the capability audit endpoint (Mode 3).

---

## Quick-Reference Commands

```bash
# Mode 1 — CI harness
python -m pitwall.audit.sixteen_check --strict

# Mode 2 — Local tests
python -m pytest tests/test_audit_sixteen_check.py -v

# Mode 3 — Capability audit endpoint
curl -s -X POST \
  -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}" \
  https://pitwall.example.com/v1/admin/audit-capability/embedding.bge-m3

# File existence gate
test -f docs/operator/16-check-audit-procedure.md && echo "exists"
```

---

## Landmine Quick Reference

| Landmine | Topic |
|---|---|
| L1 | Canonical GPU IDs |
| L2 | `cloud_type=SECURE` for volumes |
| L3 | `runtime != null` for readiness |
| L4 | SSH-first probe vs proxy 524 |
| L5 | LB endpoint creation is console-only |
| L6 | Pluggable capacity-error matcher |
| L7 | Volume attach hang (5-min timeout) |
| L8 | Idempotent fast-200 webhook + retention windows |
| L9 | FlashBoot runpodctl regression |
| L10 | Mount path divergence (Pods `/workspace`, Serverless `/runpod-volume`) |
| L11 | R2 temporary access credentials |
| L12 | `hf download` (not the legacy Hugging Face CLI invocation) |
| L13 | Dep pin in worker images |
| L14 | `workersMin=1` costs ~$43/day at the example rate |
| L15 | `terminate-pod` vs `emergency-kill` verb distinction |
| L16 | Single-axis lease mutations |
