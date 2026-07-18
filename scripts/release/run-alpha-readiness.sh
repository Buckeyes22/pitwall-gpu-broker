#!/bin/bash
# run-alpha-readiness.sh — local public-alpha release-readiness harness.
#
# Runs the exact release-marker suite and strict sixteen-check audit. Project
# publication is hermetic; optional provider-live execution belongs to operators
# using credentials and infrastructure they own.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

: "${DATABASE_URL:?DATABASE_URL environment variable is required}"
: "${REDIS_URL:?REDIS_URL environment variable is required}"

ARTIFACT_DIR="${ARTIFACT_DIR:-${REPO_ROOT}/dist/alpha-artifacts}"
PITWALL_BASE_URL="${PITWALL_BASE_URL:-}"
RUNPOD_API_KEY="${RUNPOD_API_KEY:-}"

mkdir -p "${ARTIFACT_DIR}"
ARTIFACT_DIR="$(cd "${ARTIFACT_DIR}" && pwd -P)"

log() {
    printf '[run-alpha-readiness] %s\n' "$*" >&2
}

fail() {
    printf '[run-alpha-readiness] FAIL: %s\n' "$*" >&2
    exit 1
}

export DATABASE_URL REDIS_URL ARTIFACT_DIR

if [[ -n "${PITWALL_BASE_URL}" ]]; then
    export PITWALL_BASE_URL
    log "PITWALL_BASE_URL is set"
else
    log "PITWALL_BASE_URL is not set — this is a local rehearsal, not release approval"
fi

if [[ -n "${RUNPOD_API_KEY}" ]]; then
    export RUNPOD_API_KEY
    log "RUNPOD_API_KEY is set for optional operator-owned local live tests"
else
    log "RUNPOD_API_KEY is not set — expected for hermetic project release checks"
fi

log "Artifact directory: ${ARTIFACT_DIR}"
log "Repository root: ${REPO_ROOT}"

REPORT_FILE="${ARTIFACT_DIR}/alpha-readiness-report.txt"

cd "${REPO_ROOT}"

if ! command -v uv &>/dev/null; then
    fail "uv command not found"
fi

PYTEST_VERSION=$(uv run pytest --version 2>/dev/null | head -1 || echo "pytest not installed")
log "Pytest: ${PYTEST_VERSION}"

uv run pytest tests/release/ -v -m release --tb=short 2>&1 | tee "${REPORT_FILE}"
PYTEST_EXIT_CODE=${PIPESTATUS[0]}

log "Full report written to: ${REPORT_FILE}"

if [[ ${PYTEST_EXIT_CODE} -ne 0 ]]; then
    fail "public-alpha readiness checks failed (exit code: ${PYTEST_EXIT_CODE})"
fi

log "Running 16-check RunPod audit (--strict, fatal)..."
if ! uv run python -m pitwall.audit.sixteen_check --strict | tee -a "${REPORT_FILE}"; then
    fail "16-check audit failed under --strict"
fi

log "All local hermetic public-alpha checks passed"
