# Create vLLM Endpoint — Operator Runbook

Runbook for creating a custom vLLM serverless endpoint on RunPod and registering it with Pitwall.

**Constraint (L5):** LB endpoint creation is console-only. Custom vLLM Serverless endpoint creation is API-accessible. Pitwall v1 **registers existing endpoints** — it does not create them via API. Use `pitwall-gpu-broker register-template --image` to register the template, then `pitwall-gpu-broker register-endpoint` to register the endpoint.

**Constraint (L9):** FlashBoot must be verified in the RunPod console after create. runpodctl 0.x silently no-ops `--flash-boot` on create and errors on update.

---

## Pre-flight

- [ ] Identify desired model (e.g., `Qwen/Qwen3.6-27B-FP8`, `Qwen/Qwen3-VL-32B-Instruct-FP8`).
- [ ] Confirm GPU pool availability (`ADA_80_PRO`, `H100`, etc.).
- [ ] Confirm the capability exists in `pitwall.capabilities`.

---

## Step 1 — Build and Register the Template

Use `pitwall-gpu-broker register-template --image <image-ref>` to build, push, and cache the template. See `pitwall-gpu-broker register-template --help` for flags.

Template env contract for vLLM tool-calling endpoints:

| Variable | Value | Notes |
|---|---|---|
| `MODEL_NAME` | e.g., `Qwen/Qwen3.6-27B-FP8` | HuggingFace model ID |
| `MAX_MODEL_LEN` | e.g., `32768` | Context window |
| `GPU_MEMORY_UTILIZATION` | e.g., `0.92` | vLLM memory fraction |
| `OPENAI_SERVED_MODEL_NAME_OVERRIDE` | e.g., `qwen3-6-27b` | Name exposed via OpenAI compat layer |
| `RAW_OPENAI_OUTPUT` | `1` | Raw output mode |
| `LIMIT_MM_PER_PROMPT` | e.g., `image=4` | Multimodal limit (VLM only) |
| `TRUST_REMOTE_CODE` | `true` | Required for some models |
| `ENABLE_AUTO_TOOL_CHOICE` | `true` | Auto tool selection |
| `REASONING_PARSER` | `qwen3` | Reasoning extraction |
| `TOOL_CALL_PARSER` | `hermes` | Hermes tool call format |

Example `runpodctl serverless create` equivalent:

```bash
runpodctl serverless create \
  --hub-id <hub-id> \
  --name <endpoint-name> \
  --gpu-id ADA_80_PRO \
  --workers-min 0 \
  --workers-max <N> \
  --idle-timeout 10 \
  --flash-boot \
  --env MODEL_NAME=Qwen/Qwen3.6-27B-FP8 \
  --env MAX_MODEL_LEN=32768 \
  --env GPU_MEMORY_UTILIZATION=0.92 \
  --env OPENAI_SERVED_MODEL_NAME_OVERRIDE=qwen3-6-27b \
  --env RAW_OPENAI_OUTPUT=1 \
  --env TRUST_REMOTE_CODE=true \
  --env ENABLE_AUTO_TOOL_CHOICE=true \
  --env REASONING_PARSER=qwen3 \
  --env TOOL_CALL_PARSER=hermes
```

Scaler config: `scalerType=QUEUE_DELAY`, `scalerValue=4`.

---

## Step 2 — Verify FlashBoot via Console (L9)

After endpoint creation:

1. Log into the RunPod console.
2. Navigate to the endpoint → Settings.
3. Verify **FlashBoot** is toggled **ON** in the console UI.
4. If FlashBoot is OFF: the runpodctl regression struck again. Recreate the endpoint — do not trust CLI confirmation.

---

## Step 3 — Cold-Start Expectations

| Phase | Duration |
|---|---|
| HF model download (first cold start) | 5–30 minutes for large models |
| vLLM health gate (post-download) | Up to 30 minutes |
| FlashBoot regression penalty | +~35s per cold start (if FlashBoot disabled) |

First-ever cold start on a new endpoint can take 10–60 minutes total due to image pull + HF download + vLLM model loading. Subsequent cold starts are faster with FlashBoot enabled.

---

## Step 4 — Register the Endpoint with Pitwall

Use `pitwall-gpu-broker register-endpoint` to register the endpoint. See `pitwall-gpu-broker register-endpoint --help` for required fields.

OpenAI-compatible base URL pattern: `https://api.runpod.ai/v2/{ENDPOINT_ID}/openai/v1`

---

## Step 5 — Smoke Tests

### Direct RunPod URL

```bash
curl -s -X POST "https://api.runpod.ai/v2/<endpoint-id>/openai/v1/chat/completions" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"<model-name>","messages":[{"role":"user","content":"Reply with exactly the word OK and nothing else."}],"max_tokens":8,"temperature":0.0}'
```

Expected: HTTP 200, `choices[0].message.content == "OK"`.

### Pitwall /v1/inference

```bash
curl -s -X POST "https://pitwall.example.com/v1/inference" \
  -H "Content-Type: application/json" \
  -d '{
    "capability": "<capability-id>",
    "model": "<model-name>",
    "messages": [{"role": "user", "content": "Reply with exactly the word OK."}],
    "max_tokens": 8
  }'
```

Expected: HTTP 200, model output present.

---

## Post-Create Checklist

- [ ] Endpoint ID noted
- [ ] Endpoint image, port, and scaling settings verified in the RunPod console
- [ ] `pitwall-gpu-broker register-template --image` completed
- [ ] `pitwall-gpu-broker register-endpoint` returned success
- [ ] Direct RunPod smoke test passed
- [ ] Pitwall `/v1/inference` smoke test passed
- [ ] `workersMin=0`, `idleTimeout=10`, `QUEUE_DELAY scalerValue=4` confirmed

---

## Related documentation

- [RunPod integration](../sdlc/08-runpod-integration.md)
- [Core configuration](../sdlc/16-core-config.md)
