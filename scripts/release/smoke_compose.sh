#!/bin/bash
# Canonical Compose runtime smoke for already-built release images.
#
# PITWALL_IMAGE_TAG selects the local image tag. COMPOSE_PROJECT_NAME must use
# the pitwall-* prefix so cleanup cannot target an unrelated project.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

: "${PITWALL_IMAGE_TAG:?PITWALL_IMAGE_TAG is required}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-pitwall-release-smoke}"
if [[ ! "${COMPOSE_PROJECT_NAME}" =~ ^pitwall-[A-Za-z0-9_-]+$ ]]; then
    printf 'refusing unsafe Compose project name: %s\n' "${COMPOSE_PROJECT_NAME}" >&2
    exit 2
fi
export COMPOSE_PROJECT_NAME

PITWALL_SMOKE_ARTIFACT_DIR="${PITWALL_SMOKE_ARTIFACT_DIR:-/tmp/pitwall-compose-smoke}"
mkdir -p "${PITWALL_SMOKE_ARTIFACT_DIR}"

cleanup() {
    docker compose logs --no-color >"${PITWALL_SMOKE_ARTIFACT_DIR}/compose.log" 2>&1 || true
    docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

export PITWALL_BIND_IP=127.0.0.1
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-compose-smoke-postgres}"
export REDIS_PASSWORD="${REDIS_PASSWORD:-compose-smoke-redis}"
export RUNPOD_API_KEY="${RUNPOD_API_KEY:-compose-smoke-runpod}"
export PITWALL_API_TOKEN="${PITWALL_API_TOKEN:-compose-smoke-api-token-0001}"
export PITWALL_ADMIN_SECRET="${PITWALL_ADMIN_SECRET:-compose-smoke-admin-secret-0001}"
export PITWALL_WEBHOOK_SECRET="${PITWALL_WEBHOOK_SECRET:-compose-smoke-webhook-secret-0001}"
export PITWALL_WEBHOOK_ENCRYPTION_KEYS="${PITWALL_WEBHOOK_ENCRYPTION_KEYS:-{\"v1\":\"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\"}}"
export PITWALL_ARCHIVE_ENCRYPTION_KEY="${PITWALL_ARCHIVE_ENCRYPTION_KEY:-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=}"

docker compose config -q

for service in api reconciler webhook cost-exporter mcp; do
    image="pitwall-gpu-broker/${service}:${PITWALL_IMAGE_TAG}"
    test "$(docker image inspect --format '{{.Config.User}}' "${image}")" = "10001:10001"
    test "$(docker image inspect --format '{{if .Config.Healthcheck}}yes{{end}}' "${image}")" = "yes"
done

docker compose up -d --no-build --wait --wait-timeout 180

curl --fail --silent --show-error http://127.0.0.1:8080/readyz \
    >"${PITWALL_SMOKE_ARTIFACT_DIR}/api-ready.json"
curl --fail --silent --show-error http://127.0.0.1:8082/readyz \
    >"${PITWALL_SMOKE_ARTIFACT_DIR}/webhook-ready.json"
curl --fail --silent --show-error http://127.0.0.1:9109/readyz \
    >"${PITWALL_SMOKE_ARTIFACT_DIR}/exporter-ready.json"

test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:8080/v1/leases)" = "401"

for service in api reconciler webhook cost-exporter; do
    container_id="$(docker compose ps -q "${service}")"
    test -n "${container_id}"
    test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${container_id}")" = "true"
    test "$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${container_id}")" = '["ALL"]'
    docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${container_id}" \
        | grep -q 'no-new-privileges:true'
done

docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --cap-drop ALL --security-opt no-new-privileges \
    "pitwall-gpu-broker/mcp:${PITWALL_IMAGE_TAG}" pitwall-gpu-broker --version \
    >"${PITWALL_SMOKE_ARTIFACT_DIR}/mcp-version.txt"

docker compose stop -t 10
docker compose logs --no-color >"${PITWALL_SMOKE_ARTIFACT_DIR}/compose.log"
for service in api webhook cost-exporter; do
    grep -q "${service}-1.*Application shutdown complete" \
        "${PITWALL_SMOKE_ARTIFACT_DIR}/compose.log"
done

printf 'canonical Compose runtime smoke passed\n'
