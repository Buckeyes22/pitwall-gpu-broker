# Create LB Endpoint — Operator Runbook

Runbook for creating a RunPod load-balancer (LB) endpoint and registering it with Pitwall.

**Constraint:** Pitwall registers existing LB endpoints; it does not create them.

---

## Pre-flight

- [ ] Confirm the desired capability (`capability_id`) is defined in `pitwall.capabilities`.
- [ ] Confirm no existing provider satisfies the capability; if one exists and is failing, resolve before adding a second.
- [ ] Identify the target GPU pool and expected traffic pattern (hot/always-on vs cold/batch).
- [ ] For OpenAI-compatible vLLM endpoints: confirm the env shape (`MODEL_NAME`, `MAX_MODEL_LEN`, `OPENAI_SERVED_MODEL_NAME_OVERRIDE`, etc.) matches the worker image.

---

## Step 1 — Build and Push the Docker Image

Build your custom image and push to a registry accessible to RunPod (e.g., `ghcr.io`).

```bash
docker buildx build --push \
  --tag ghcr.io/<org>/<image>:<tag> \
  --platform linux/amd64 \
  .
```

**Example deployment annotation (not a default):**

- Image: `ghcr.io/example/pitwall-embed:1`
- Baked BGE-M3 weights at build time; network volumes are globally broken on RunPod.
- Custom FastAPI server exposing `/embed` + `/ping`.

For vLLM endpoints, supply an independently reviewed image that implements the
RunPod endpoint protocol. Pitwall does not publish or support a GPU worker image
in the public alpha; model licensing, image provenance, and runtime hardening are
the operator's responsibility.

---

## Step 2 — Create the LB Endpoint via RunPod Console

Create the endpoint in the RunPod console, then register the resulting endpoint
ID with Pitwall.

### Console steps

1. Navigate to RunPod console → Serverless → Deploy.
2. Select **Custom (Docker image)**.
3. Provide the registry auth (e.g., GitHub PAT stored in `~/.docker/config.json` on the operator host).
4. Enter your image URI (e.g., `ghcr.io/example/pitwall-embed:1`).
5. Select **Load-balancer** as the endpoint type (not queue). Queue endpoints have different scaling semantics and different `/openai/v1/...` URL shapes.
6. Select GPU pool (e.g., `ADA_80_PRO`).
7. Configure:
   - **workersMin**: `1` for hot/always-on; `0` for batch/cold. A nonzero minimum incurs continuous provider charges, so check current pricing and set it back to `0` when it is not needed.
   - **workersMax**: `4` (example; tune to expected parallelism).
   - **idleTimeout**: `60s` (example).
   - **scalerType**: `REQUEST_COUNT` (LB) or `QUEUE_DELAY` (queue).
   - **scalerValue**: `4` (example).
8. Expose HTTP port (e.g., `80`).
9. Add any required env vars (e.g., `MODEL_NAME`, `OPENAI_SERVED_MODEL_NAME_OVERRIDE` for vLLM).
10. Click **Deploy**.

### After Deploy

- Note the **endpoint ID** (e.g., `eptest00000000` in the example).
- Note the base URL: `https://<endpoint_id>.api.runpod.ai`.
- Smoke-test: `curl https://<endpoint_id>.api.runpod.ai/ping` (or `/embed` for custom HTTP endpoints).

**Example deployment annotation:**

| Field | Value |
|---|---|
| Name | `example-embed-bge-m3-batch` |
| Endpoint ID | `eptest00000000` |
| Type | **Load-balancer** |
| Image | `ghcr.io/example/pitwall-embed:1` |
| GPU pool | `ADA_80_PRO` |
| Scaling | `workersMin=1`, `workersMax=4`, `idleTimeout=60s`, `scalerType=REQUEST_COUNT`, `scalerValue=4` |
| URLs | `https://eptest00000000.api.runpod.ai/embed`, `/ping` |

---

## Step 3 — Verify endpoint settings

After endpoint creation, verify the saved settings in the provider console:

1. Log into the RunPod console.
2. Navigate to the endpoint → Settings.
3. Verify the image, scaling limits, exposed port, and any acceleration options.
4. Correct any mismatch before registering the endpoint with Pitwall.

This step applies to both queue and LB endpoint types.

---

## Step 4 — Register the Provider with Pitwall

After the endpoint exists and is smoke-tested, register it with Pitwall via `POST /v1/admin/providers`.

```bash
curl -s -X POST \
  -H "X-Pitwall-Secret: $PITWALL_ADMIN_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{
    "capability_id": "<capability_id>",
    "name": "<display-name>",
    "provider_type": "runpod_serverless",
    "runpod_endpoint_id": "<endpoint_id>",
    "region": "us-east-1",
    "cloud_type": "runpod",
    "config": {
      "base_url": "https://<endpoint_id>.api.runpod.ai",
      "gpu_class": "ADA_80_PRO",
      "workers_min": 1,
      "workers_max": 4,
      "idle_timeout": 60,
      "flashboot": true
    }
  }' \
  https://pitwall.example.com/v1/admin/providers
```

**Fields:**

| Field | Required | Notes |
|---|---|---|
| `capability_id` | Yes | Must match an existing `pitwall.capabilities.id` |
| `name` | Yes | Human-readable, e.g., `embed-bge-m3-us` |
| `provider_type` | Yes | `runpod_serverless` for LB and queue endpoints |
| `runpod_endpoint_id` | Yes | The endpoint ID from Step 2 |
| `config.base_url` | Yes | `https://<endpoint_id>.api.runpod.ai` |
| `config.gpu_class` | Recommended | e.g., `ADA_80_PRO` |
| `config.workers_min` | Yes | `0` for cold/batch; `1` for hot |
| `config.workers_max` | Yes | Max parallelism |
| `config.flashboot` | Recommended | Capture FlashBoot state for audit |

---

## Step 5 — Audit the Provider

Run the capability audit to verify the new provider passes checks:

```bash
curl -s -X POST \
  -H "X-Pitwall-Secret: $PITWALL_ADMIN_SECRET" \
  https://pitwall.example.com/v1/admin/audit-capability/<capability_id>
```

Expected: HTTP 200 with an audit report showing the new provider passing health, sovereignty, and budget checks.

If the audit fails, inspect the response for the specific failure reason and remediate before routing traffic to the new provider.

---

## Step 6 — Confirm workersMin Is Set Appropriately

Before going live, confirm `workersMin` matches the traffic pattern:

- **Hot/always-on (e.g., API query path):** `workersMin=1`. This incurs continuous provider charges.
- **Cold/batch:** `workersMin=0`. Pitwall's daily hibernate sweep will alert if `workersMin > 0` for >24h on a registered LB endpoint.

To hibernate an endpoint immediately:

```bash
curl -s -X POST \
  -H "X-Pitwall-Secret: $PITWALL_ADMIN_SECRET" \
  https://pitwall.example.com/v1/admin/providers/<provider_id>/hibernate
```

---

## Post-Create Checklist

- [ ] Endpoint ID noted and smoke-tested
- [ ] Endpoint image, port, and scaling settings verified in the RunPod console
- [ ] `POST /v1/admin/providers` returned 201 with provider ID
- [ ] `POST /v1/admin/audit-capability/{name}` returned 200
- [ ] `workersMin` set to intended value (0 for batch, 1 for hot)
- [ ] If `workersMin=1`: confirmed continuous cost and scheduled a reminder to set it back to 0 when done
- [ ] Capability routing updated to include new provider if applicable

---

## Related documentation

- [RunPod integration](../sdlc/08-runpod-integration.md)
- [Core configuration](../sdlc/16-core-config.md)
