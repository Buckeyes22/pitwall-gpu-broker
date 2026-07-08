#!/bin/bash
# run-user-journeys.sh — executable user-journey suite for Pitwall.
#
# Walks every hermetic journey from docs/operator/user-journey-catalog.md the
# way a new user would: real CLI invocations, real servers on local ports,
# real HTTP calls. Never uses a real RunPod key; never creates paid resources.
#
# Required env vars:
#   DATABASE_URL   — disposable local Postgres (the suite resets its schema)
#   REDIS_URL      — local Redis
#
# Exit codes: 0 all journeys pass; 1 one or more failed.

set -uo pipefail

: "${DATABASE_URL:?DATABASE_URL is required (disposable local database)}"
: "${REDIS_URL:?REDIS_URL is required (local redis)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

export RUNPOD_API_KEY="${RUNPOD_API_KEY:-local-dry-run-key}"
export PITWALL_ADMIN_SECRET="${PITWALL_ADMIN_SECRET:-journey-admin-secret}"

API_PORT=18080
WEBHOOK_PORT=18082
AUTH_API_PORT=18083
RATE_API_PORT=18084
BUDGET_API_PORT=18085
EXPORTER_PORT=18090

# Fail fast if any journey port is already bound — otherwise the suite would
# silently exercise a stale server from a previous (crashed) run.
for port in ${API_PORT} ${WEBHOOK_PORT} ${AUTH_API_PORT} ${RATE_API_PORT} ${BUDGET_API_PORT} ${EXPORTER_PORT}; do
    if ss -tln 2>/dev/null | grep -q ":${port} "; then
        printf '[journeys] FATAL: port %s already in use — kill the stale process first\n' "${port}" >&2
        exit 2
    fi
done

ARTIFACTS="$(mktemp -d "${TMPDIR:-/tmp}/pitwall-journeys.XXXXXX")"
PIDS=()
FAILED=0
PASSED=0
declare -a RESULTS=()

cleanup() {
    for pid in "${PIDS[@]:-}"; do
        [ -n "${pid}" ] && kill "${pid}" >/dev/null 2>&1
    done
    wait >/dev/null 2>&1
}
trap cleanup EXIT

log()  { printf '[journeys] %s\n' "$*" >&2; }

journey_pass() { PASSED=$((PASSED+1)); RESULTS+=("PASS  $1"); log "PASS  $1"; }
journey_fail() { FAILED=$((FAILED+1)); RESULTS+=("FAIL  $1 — $2"); log "FAIL  $1 — $2"; }

# check <journey-id> <description> <command...>  — records first failure per call
check() {
    local id="$1" desc="$2"; shift 2
    if "$@" >"${ARTIFACTS}/last.out" 2>&1; then
        return 0
    fi
    journey_fail "${id}" "${desc} (see below)"
    sed 's/^/    /' "${ARTIFACTS}/last.out" | tail -15 >&2
    return 1
}

# expect_fail <journey-id> <description> <command...> — inverted check
expect_fail() {
    local id="$1" desc="$2"; shift 2
    if "$@" >"${ARTIFACTS}/last.out" 2>&1; then
        journey_fail "${id}" "${desc}: expected non-zero exit, got success"
        return 1
    fi
    return 0
}

http_code() { curl -s -o "${ARTIFACTS}/body.json" -w '%{http_code}' "$@"; }

# json_assert <file> <python-expression over parsed `d`>
json_assert() {
    JOURNEY_JSON="$1" JOURNEY_EXPR="$2" uv run python -c "
import json, os
d = json.load(open(os.environ['JOURNEY_JSON']))
expr = os.environ['JOURNEY_EXPR']
assert eval(expr), 'assertion failed: ' + expr + ' on ' + json.dumps(d)[:400]
"
}

db_query() { # db_query <sql> — prints rows as python tuples
    JOURNEY_SQL="$1" uv run python -c "
import asyncio, asyncpg, os
async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    try:
        for row in await conn.fetch(os.environ['JOURNEY_SQL']):
            print(tuple(row))
    finally:
        await conn.close()
asyncio.run(main())
"
}

db_expect() { db_query "$1" | grep -q "$2"; }

wait_for_url() { # wait_for_url <url> <seconds>
    local url="$1" deadline=$(( $(date +%s) + $2 ))
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        if curl -sf "${url}" >/dev/null 2>&1; then return 0; fi
        sleep 0.5
    done
    return 1
}

# start_bg <logfile> <command...> — records the PID in the parent shell's PIDS
# array so the EXIT trap can kill it. Never call this inside $(...) — command
# substitution forks a subshell and the PID registration would be lost,
# leaking the server past cleanup.
start_bg() {
    local logfile="$1"; shift
    "$@" >"${ARTIFACTS}/${logfile}" 2>&1 &
    PIDS+=($!)
}

fresh_db() {
    uv run pitwall-gpu-broker db reset --force >/dev/null 2>&1
    uv run pitwall-gpu-broker db migrate >/dev/null 2>&1
}

log "artifacts: ${ARTIFACTS}"

# ---------------------------------------------------------------- J01 quickstart
j01() {
    local id=J01 ok=1
    fresh_db
    check ${id} "pitwall init --non-interactive" \
        uv run pitwall-gpu-broker init --non-interactive || ok=0
    PITWALL_API_PORT=${API_PORT} start_bg api.log uv run pitwall-api
    check ${id} "API becomes healthy" \
        wait_for_url "http://127.0.0.1:${API_PORT}/healthz" 30 || ok=0
    local code
    code=$(http_code -X POST "http://127.0.0.1:${API_PORT}/v1/inference" \
        -H 'Content-Type: application/json' \
        -d '{"capability":"embedding.demo","texts":["hello"],"dry_run":true}')
    if [ "${code}" != "200" ]; then
        journey_fail ${id} "dry-run inference HTTP ${code}: $(head -c 300 "${ARTIFACTS}/body.json")"; ok=0
    else
        check ${id} "dry-run response fields" json_assert "${ARTIFACTS}/body.json" \
            "d['result']['dry_run'] is True and d['result']['selected_provider_id'] == 'prov_demo_runpod_lb'" || ok=0
    fi
    [ ${ok} -eq 1 ] && journey_pass "${id} README quick start"
}

# ------------------------------------------------- J02 init variants
j02() {
    local id=J02 ok=1
    fresh_db
    check ${id} "init --from-seed" \
        uv run pitwall-gpu-broker init --non-interactive --from-seed seed || ok=0
    check ${id} "init manual flags" \
        uv run pitwall-gpu-broker init --non-interactive --manual \
            --capability-name embedding.manual --capability-class embedding \
            --cost-mode per_second --provider-name manual-lb \
            --endpoint-id eptest00000009 --provider-type serverless_lb \
            --region US-EXAMPLE-1 --gpu-class "NVIDIA L4" --per-second-active 0.001 || ok=0
    check ${id} "manual capability row exists" \
        db_expect "SELECT name FROM pitwall.capabilities WHERE name='embedding.manual'" embedding.manual || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} init variants"
}

# ------------------------------------------------- J03 seed command
j03() {
    local id=J03 ok=1
    fresh_db
    check ${id} "seed with --mark-healthy" \
        uv run pitwall-gpu-broker seed seed/capabilities.yaml seed/providers.yaml --mark-healthy || ok=0
    check ${id} "seeded provider is healthy" \
        db_expect "SELECT health_status FROM pitwall.providers WHERE id='prov_demo_runpod_lb'" healthy || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} seed command"
}

# ------------------------------------------------- J04 manual onboarding
j04() {
    local id=J04 ok=1
    fresh_db
    check ${id} "create-capability" \
        uv run pitwall-gpu-broker create-capability --name embedding.manual2 --class embedding --cost-mode per_second || ok=0
    local capid
    capid=$(db_query "SELECT id FROM pitwall.capabilities WHERE name='embedding.manual2'" | tr -d "(',)")
    if [ -z "${capid}" ]; then journey_fail ${id} "capability id not found"; ok=0; fi
    check ${id} "register-endpoint" \
        uv run pitwall-gpu-broker register-endpoint --endpoint-id eptest00000008 \
            --provider-type serverless_lb --capability-id "${capid}" \
            --name manual-endpoint --gpu-class "NVIDIA L4" --region US-EXAMPLE-1 \
            --cost-mode per_second --per-second-active 0.001 || ok=0
    local provid
    provid=$(db_query "SELECT id FROM pitwall.providers WHERE name='manual-endpoint'" | tr -d "(',)")
    check ${id} "set-provider-health healthy" \
        uv run pitwall-gpu-broker set-provider-health "${provid}" healthy || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} manual onboarding"
}

# ------------------------------------------------- J05 config check
j05() {
    local id=J05 ok=1
    check ${id} "config check with full env" uv run pitwall-gpu-broker config check || ok=0
    expect_fail ${id} "config check fails closed without DATABASE_URL" \
        env -u DATABASE_URL uv run pitwall-gpu-broker config check || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} config check"
}

# ------------------------------------------------- J06 db lifecycle
j06() {
    local id=J06 ok=1
    check ${id} "db status" uv run pitwall-gpu-broker db status || ok=0
    check ${id} "db migrate idempotent" uv run pitwall-gpu-broker db migrate || ok=0
    expect_fail ${id} "reset refused without --force" uv run pitwall-gpu-broker db reset || ok=0
    expect_fail ${id} "reset refused for remote host even with --force" \
        env DATABASE_URL='postgresql://u:p@db.example.com:5432/prod' uv run pitwall-gpu-broker db reset --force || ok=0
    check ${id} "reset allowed locally with --force" uv run pitwall-gpu-broker db reset --force || ok=0
    check ${id} "re-migrate after reset" uv run pitwall-gpu-broker db migrate || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} db lifecycle guardrails"
}

# ------------------------------------------------- J07 API discovery (uses J01 API)
j07() {
    local id=J07 ok=1 base="http://127.0.0.1:${API_PORT}"
    fresh_db
    uv run pitwall-gpu-broker init --non-interactive >/dev/null 2>&1
    for path in /healthz /health /v1/health /v1/capabilities /v1/providers /openapi.json /docs; do
        local code; code=$(http_code "${base}${path}")
        if [ "${code}" != "200" ]; then journey_fail ${id} "GET ${path} -> ${code}"; ok=0; fi
    done
    local code
    code=$(http_code "${base}/v1/capabilities/embedding.demo")
    [ "${code}" = "200" ] || { journey_fail ${id} "capability by name -> ${code}"; ok=0; }
    local provid="prov_demo_runpod_lb"
    code=$(http_code "${base}/v1/providers/${provid}")
    [ "${code}" = "200" ] || { journey_fail ${id} "provider by id -> ${code}"; ok=0; }
    code=$(http_code "${base}/v1/providers/${provid}/health")
    [ "${code}" = "200" ] || { journey_fail ${id} "provider health -> ${code}"; ok=0; }
    [ ${ok} -eq 1 ] && journey_pass "${id} API discovery"
}

# ------------------------------------------------- J08 inference error shapes
j08() {
    local id=J08 ok=1 base="http://127.0.0.1:${API_PORT}"
    local code
    code=$(http_code -X POST "${base}/v1/inference" -H 'Content-Type: application/json' \
        -d '{"capability":"does.not.exist","texts":["x"],"dry_run":true}')
    [ "${code}" = "404" ] || { journey_fail ${id} "unknown capability -> ${code} (want 404)"; ok=0; }
    code=$(http_code -X POST "${base}/v1/inference" -H 'Content-Type: application/json' -d '{"nope":1}')
    [ "${code}" = "422" ] || { journey_fail ${id} "malformed body -> ${code} (want 422)"; ok=0; }
    [ ${ok} -eq 1 ] && journey_pass "${id} inference error shapes"
}

# ------------------------------------------------- J09 TUI boot
j09() {
    local id=J09 ok=1
    script -qec "timeout 6 uv run pitwall-gpu-broker dashboard" "${ARTIFACTS}/tui.typescript" \
        >/dev/null 2>&1
    if grep -q "Traceback" "${ARTIFACTS}/tui.typescript"; then
        journey_fail ${id} "TUI crashed: $(grep -m1 -A2 Traceback "${ARTIFACTS}/tui.typescript" | tr '\n' ' ' | head -c 200)"
        ok=0
    fi
    [ ${ok} -eq 1 ] && journey_pass "${id} TUI dashboard boots"
}

# ------------------------------------------------- J10 MCP stdio
j10() {
    local id=J10
    if check ${id} "MCP stdio session" uv run python - <<'PY'
import asyncio, json, sys

async def main():
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    import os
    params = StdioServerParameters(command="uv", args=["run", "pitwall-mcp"], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        info = await s.initialize()
        assert info.serverInfo.name == "pitwall", info.serverInfo
        tools = (await s.list_tools()).tools
        assert len(tools) >= 20, f"only {len(tools)} tools"
        caps = await s.call_tool("pitwall_list_capabilities", {})
        assert not caps.isError, caps
        caps_text = "".join(c.text for c in caps.content if hasattr(c, "text"))
        assert "embedding.demo" in caps_text, caps_text[:300]
        res = await s.call_tool(
            "pitwall_submit_inference",
            {"capability_id": "cap_embedding_demo", "dry_run": True, "payload": {"texts": ["hello"]}},
        )
        assert not res.isError, res
        text = "".join(c.text for c in res.content if hasattr(c, "text"))
        assert "dry_run" in text, text[:300]

asyncio.run(asyncio.wait_for(main(), timeout=60))
print("mcp stdio journey ok")
PY
    then journey_pass "${id} MCP stdio"; fi
}

# ------------------------------------------------- J11 network MCP rejected
j11() {
    local id=J11
    if PITWALL_MCP_TRANSPORT=sse uv run pitwall-mcp >"${ARTIFACTS}/mcp-network-rejected.log" 2>&1; then
        journey_fail "${id}" "unauthenticated MCP network transport unexpectedly started"
        return
    fi
    if grep -Eq "network MCP transports are unavailable|Input should be 'stdio'" \
        "${ARTIFACTS}/mcp-network-rejected.log"; then
        journey_pass "${id} network MCP fails closed"
    else
        journey_fail "${id}" "network refusal was not explicit"
    fi
}

# ------------------------------------------------- J12 admin API
j12() {
    local id=J12 ok=1 base="http://127.0.0.1:${API_PORT}"
    local code
    code=$(http_code -X POST "${base}/v1/admin/capabilities" -H 'Content-Type: application/json' -d '{}')
    case "${code}" in 401|403) : ;; *) journey_fail ${id} "no secret -> ${code} (want 401/403)"; ok=0 ;; esac
    code=$(http_code -X POST "${base}/v1/admin/capabilities" \
        -H "X-Pitwall-Secret: wrong-secret" -H 'Content-Type: application/json' -d '{}')
    case "${code}" in 401|403) : ;; *) journey_fail ${id} "wrong secret -> ${code} (want 401/403)"; ok=0 ;; esac
    local body='{"name":"embedding.adminj","version":"1.0.0","class":"embedding","cost_mode":"per_request"}'
    code=$(http_code -X POST "${base}/v1/admin/capabilities" \
        -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}" -H 'Content-Type: application/json' -d "${body}")
    [ "${code}" = "201" ] || { journey_fail ${id} "create capability -> ${code}: $(head -c 200 "${ARTIFACTS}/body.json")"; ok=0; }
    local capid
    capid=$(uv run python -c "import json;print(json.load(open('${ARTIFACTS}/body.json')).get('id',''))" 2>/dev/null)
    if [ -n "${capid}" ]; then
        code=$(http_code -X POST "${base}/v1/admin/capabilities/${capid}/disable" \
            -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}")
        [ "${code}" = "200" ] || { journey_fail ${id} "disable -> ${code}"; ok=0; }
        code=$(http_code -X POST "${base}/v1/admin/capabilities/${capid}/enable" \
            -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}")
        [ "${code}" = "200" ] || { journey_fail ${id} "enable -> ${code}"; ok=0; }
    fi
    code=$(http_code -X POST "${base}/v1/admin/audit-capability/embedding.demo" \
        -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}")
    [ "${code}" = "200" ] || { journey_fail ${id} "audit-capability -> ${code}"; ok=0; }
    [ ${ok} -eq 1 ] && journey_pass "${id} admin API"
}

# ------------------------------------------------- J13 whole-API auth
j13() {
    local id=J13 ok=1
    PITWALL_API_PORT=${AUTH_API_PORT} PITWALL_API_TOKEN=journey-token \
        start_bg api-auth.log uv run pitwall-api
    check ${id} "auth API healthy (health is public)" \
        wait_for_url "http://127.0.0.1:${AUTH_API_PORT}/healthz" 30 || ok=0
    local code
    code=$(http_code "http://127.0.0.1:${AUTH_API_PORT}/v1/capabilities")
    [ "${code}" = "401" ] || { journey_fail ${id} "no bearer -> ${code} (want 401)"; ok=0; }
    code=$(http_code -H "Authorization: Bearer journey-token" "http://127.0.0.1:${AUTH_API_PORT}/v1/capabilities")
    [ "${code}" = "200" ] || { journey_fail ${id} "with bearer -> ${code} (want 200)"; ok=0; }
    [ ${ok} -eq 1 ] && journey_pass "${id} whole-API token auth"
}

# ------------------------------------------------- J14 rate limit
j14() {
    local id=J14 ok=1
    PITWALL_API_PORT=${RATE_API_PORT} PITWALL_INBOUND_RATE_LIMIT=3/60s \
        start_bg api-rate.log uv run pitwall-api
    check ${id} "rate-limited API healthy" \
        wait_for_url "http://127.0.0.1:${RATE_API_PORT}/healthz" 30 || ok=0
    local got429=0
    for _ in 1 2 3 4 5 6; do
        local code; code=$(http_code "http://127.0.0.1:${RATE_API_PORT}/v1/capabilities")
        if [ "${code}" = "429" ]; then
            got429=1
            grep -qi 'retry-after' <(curl -s -D- -o /dev/null "http://127.0.0.1:${RATE_API_PORT}/v1/capabilities") \
                || { journey_fail ${id} "429 without Retry-After"; ok=0; }
            break
        fi
    done
    [ ${got429} -eq 1 ] || { journey_fail ${id} "never saw 429 in a 6-request burst at 3/60s"; ok=0; }
    [ ${ok} -eq 1 ] && journey_pass "${id} inbound rate limit"
}

# ------------------------------------------------- J15 budget gate 402
j15() {
    local id=J15 ok=1
    PITWALL_API_PORT=${BUDGET_API_PORT} PITWALL_MONTHLY_BUDGET_USD=0.000001 \
        start_bg api-budget.log uv run pitwall-api
    check ${id} "budget API healthy" \
        wait_for_url "http://127.0.0.1:${BUDGET_API_PORT}/healthz" 30 || ok=0
    local code
    code=$(http_code -X POST "http://127.0.0.1:${BUDGET_API_PORT}/v1/inference" \
        -H 'Content-Type: application/json' \
        -d '{"capability":"embedding.demo","texts":["hello"]}')
    [ "${code}" = "402" ] || { journey_fail ${id} "exhausted budget -> ${code} (want 402): $(head -c 200 "${ARTIFACTS}/body.json")"; ok=0; }
    [ ${ok} -eq 1 ] && journey_pass "${id} budget gate rejects pre-spend"
}

# ------------------------------------------------- J16 openai proxy safety
j16() {
    local id=J16 ok=1
    local code
    code=$(http_code -X POST \
        "http://127.0.0.1:${API_PORT}/v1/openai/embedding.demo/v1/https://169.254.169.254/latest/meta-data" \
        -H 'Content-Type: application/json' -d '{}')
    case "${code}" in 4*) : ;; *) journey_fail ${id} "SSRF path -> ${code} (want 4xx)"; ok=0 ;; esac
    code=$(http_code -X POST \
        "http://127.0.0.1:${BUDGET_API_PORT}/v1/openai/embedding.demo/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d '{"model":"m","messages":[{"role":"user","content":"hi"}]}')
    [ "${code}" = "402" ] || { journey_fail ${id} "proxy with exhausted budget -> ${code} (want 402)"; ok=0; }
    [ ${ok} -eq 1 ] && journey_pass "${id} OpenAI proxy safety"
}

# ------------------------------------------------- J17 job error shapes
j17() {
    local id=J17 ok=1 base="http://127.0.0.1:${API_PORT}"
    for pathspec in "GET /v1/jobs/wkl_doesnotexist/status" "GET /v1/jobs/wkl_doesnotexist/result" "POST /v1/jobs/wkl_doesnotexist/cancel"; do
        local method="${pathspec%% *}" path="${pathspec#* }"
        local code; code=$(http_code -X "${method}" "${base}${path}")
        [ "${code}" = "404" ] || { journey_fail ${id} "${method} ${path} -> ${code} (want 404)"; ok=0; }
    done
    [ ${ok} -eq 1 ] && journey_pass "${id} job error shapes"
}

# ------------------------------------------------- J18 webhook receiver
j18() {
    local id=J18 ok=1
    PITWALL_WEBHOOK_RECEIVER_PORT=${WEBHOOK_PORT} PITWALL_WEBHOOK_SECRET=journey-webhook-secret \
        start_bg webhook.log uv run pitwall-webhook
    check ${id} "webhook receiver healthy" \
        wait_for_url "http://127.0.0.1:${WEBHOOK_PORT}/healthz" 30 || ok=0
    local payload='{"id":"job-journey-1","status":"COMPLETED"}'
    local code
    code=$(http_code -X POST "http://127.0.0.1:${WEBHOOK_PORT}/webhooks/runpod" \
        -H 'Content-Type: application/json' -d "${payload}")
    [ "${code}" = "401" ] || { journey_fail ${id} "unsigned -> ${code} (want 401)"; ok=0; }
    local sig
    sig=$(uv run python -c "
from pitwall.webhook_dispatcher.signer import sign
print(sign(b'${payload}', 'journey-webhook-secret'))
")
    code=$(http_code -X POST "http://127.0.0.1:${WEBHOOK_PORT}/webhooks/runpod" \
        -H 'Content-Type: application/json' -H "X-Pitwall-Webhook-Signature: ${sig}" -d "${payload}")
    [ "${code}" = "200" ] || { journey_fail ${id} "signed -> ${code} (want 200): $(head -c 200 "${ARTIFACTS}/body.json")"; ok=0; }
    code=$(http_code -X POST "http://127.0.0.1:${WEBHOOK_PORT}/webhooks/runpod" \
        -H 'Content-Type: application/json' -H "X-Pitwall-Webhook-Signature: ${sig}" -d "${payload}")
    if [ "${code}" = "200" ]; then
        check ${id} "replay flagged duplicate" json_assert "${ARTIFACTS}/body.json" \
            "d.get('duplicate') is True" || ok=0
    else
        journey_fail ${id} "replay -> ${code} (want 200)"; ok=0
    fi
    [ ${ok} -eq 1 ] && journey_pass "${id} webhook receiver"
}

# ------------------------------------------------- J19 reconciler
j19() {
    local id=J19 ok=1
    check ${id} "reconciler check (valid REDIS_URL)" \
        uv run python -m pitwall.reconciler check || ok=0
    expect_fail ${id} "reconciler check fails on invalid REDIS_URL" \
        env REDIS_URL='not-a-dsn' uv run python -m pitwall.reconciler check || ok=0
    timeout 8 uv run pitwall-reconciler >"${ARTIFACTS}/reconciler.log" 2>&1
    local rc=$?
    if [ ${rc} -ne 124 ]; then
        journey_fail ${id} "worker exited early rc=${rc}: $(tail -3 "${ARTIFACTS}/reconciler.log" | tr '\n' ' ' | head -c 250)"
        ok=0
    fi
    if grep -q "Traceback" "${ARTIFACTS}/reconciler.log"; then
        journey_fail ${id} "worker boot traceback: $(grep -m1 -A2 Traceback "${ARTIFACTS}/reconciler.log" | tr '\n' ' ' | head -c 250)"
        ok=0
    fi
    [ ${ok} -eq 1 ] && journey_pass "${id} reconciler check + worker boot"
}

# ------------------------------------------------- J20 cost exporter
j20() {
    local id=J20 ok=1
    PITWALL_COST_EXPORTER_PORT=${EXPORTER_PORT} \
        start_bg exporter.log uv run pitwall-cost-exporter
    check ${id} "exporter healthy" \
        wait_for_url "http://127.0.0.1:${EXPORTER_PORT}/metrics" 30 || ok=0
    if ! curl -s "http://127.0.0.1:${EXPORTER_PORT}/metrics" | grep -q "^pitwall_"; then
        journey_fail ${id} "no pitwall_ metrics in /metrics"; ok=0
    fi
    [ ${ok} -eq 1 ] && journey_pass "${id} cost exporter"
}

# ------------------------------------------------- J21 kill switch (dev, no pods)
j21() {
    local id=J21 ok=1 base="http://127.0.0.1:${API_PORT}"
    local code
    code=$(http_code -X POST "${base}/v1/admin/kill-switch" -H 'Content-Type: application/json' \
        -d '{"reason":"journey drill"}')
    case "${code}" in 401|403) : ;; *) journey_fail ${id} "unauthenticated kill -> ${code}"; ok=0 ;; esac
    code=$(http_code -X POST "${base}/v1/admin/kill-switch" \
        -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}" -H 'Content-Type: application/json' \
        -d '{"reason":"journey drill","terminate_compute":false}')
    [ "${code}" = "200" ] || { journey_fail ${id} "kill drill -> ${code}: $(head -c 250 "${ARTIFACTS}/body.json")"; ok=0; }
    check ${id} "API healthy after drill" \
        wait_for_url "${base}/healthz" 10 || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} kill switch drill"
}

# ------------------------------------------------- J22 warm-volume guardrails
j22() {
    local id=J22 ok=1
    local capid
    capid=$(db_query "SELECT id FROM pitwall.capabilities WHERE name='embedding.demo'" | tr -d "(',)")
    check ${id} "warm-volume --dry-run (no spend)" \
        uv run pitwall-gpu-broker warm-volume --capability "${capid}" --volume-id vol-journey --dry-run || ok=0
    expect_fail ${id} "warm-volume with fake key fails cleanly" \
        timeout 60 uv run pitwall-gpu-broker warm-volume --capability "${capid}" --volume-id vol-journey --timeout 20 || ok=0
    local leaked
    leaked=$(db_query "SELECT count(*) FROM pitwall.workloads WHERE type='warm_volume' AND state NOT IN ('completed','failed','cancelled','timed_out')" | tr -d "(,)")
    if [ "${leaked}" != "0" ]; then
        journey_fail ${id} "warm-volume left ${leaked} non-terminal workload(s) — reservation leak"; ok=0
    fi
    local open_cost
    open_cost=$(db_query "SELECT count(*) FROM pitwall.workloads WHERE type='warm_volume' AND state='failed' AND cost_actual_usd <> 0" | tr -d "(,)")
    if [ "${open_cost}" != "0" ]; then
        journey_fail ${id} "failed warm-volume workload has non-zero actual cost"; ok=0
    fi
    [ ${ok} -eq 1 ] && journey_pass "${id} warm-volume guardrails"
}

# ------------------------------------------------- J23 fake-key CLI errors
j23() {
    local id=J23 ok=1
    uv run pitwall-gpu-broker terminate-pod pod-does-not-exist >"${ARTIFACTS}/term.out" 2>&1
    if grep -q "Traceback" "${ARTIFACTS}/term.out"; then
        journey_fail ${id} "terminate-pod traceback"; ok=0
    fi
    expect_fail ${id} "register-template without args is a usage error" \
        uv run pitwall-gpu-broker register-template || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} fake-key CLI error handling"
}

# ------------------------------------------------- J24 canonical deployment + env docs
j24() {
    local id=J24 ok=1
    check ${id} "canonical compose validates" \
        env \
            POSTGRES_PASSWORD=journey-postgres-password \
            REDIS_PASSWORD=journey-redis-password \
            RUNPOD_API_KEY=journey-runpod-key \
            PITWALL_API_TOKEN=journey-api-token-0001 \
            PITWALL_ADMIN_SECRET=journey-admin-secret-0001 \
            PITWALL_WEBHOOK_SECRET=journey-webhook-secret-0001 \
            PITWALL_WEBHOOK_ENCRYPTION_KEYS='{"v1":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}' \
            PITWALL_ARCHIVE_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
            docker compose -f docker-compose.yml config -q || ok=0
    for var in DATABASE_URL REDIS_URL RUNPOD_API_KEY PITWALL_ADMIN_SECRET PITWALL_WEBHOOK_SECRET PITWALL_MONTHLY_BUDGET_USD; do
        grep -q "^${var}=" .env.example || { journey_fail ${id} ".env.example missing ${var}"; ok=0; }
    done
    [ ${ok} -eq 1 ] && journey_pass "${id} canonical deployment + env docs"
}

# ------------------------------------------------- J25 README testing commands
j25() {
    local id=J25 ok=1
    check ${id} "README unit lane" \
        uv run pytest -q -m "not integration and not slow" || ok=0
    check ${id} "README security lane" \
        uv run pytest -q -m "security and not fuzz" tests/security || ok=0
    [ ${ok} -eq 1 ] && journey_pass "${id} README testing commands"
}

# ---------------------------------------------------------------- run
j01; j02; j03; j04; j05; j06
j07; j08; j09; j10; j11; j12; j13; j14; j15; j16; j17
j18; j19; j20; j21; j22; j23; j24; j25

log ""
log "==== journey summary ===="
for r in "${RESULTS[@]}"; do log "  ${r}"; done
log "${PASSED} passed, ${FAILED} failed"
[ ${FAILED} -eq 0 ] || exit 1
exit 0
