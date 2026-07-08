# ADR 0002: Defer the in-repository GPU worker

- Status: Accepted for the public alpha
- Date: 2026-07-17
- Decision owner: Runtime maintainer

## Context

The former `worker-vllm` image started vLLM and then invoked
`python -m pitwall.worker`. That module only parsed and printed configuration;
it did not consume a queue, acknowledge work, persist results, retry failures,
handle cancellation, or maintain heartbeats. The process then exited zero. A
published image could therefore look successfully configured while doing no
work and could leave paid GPU capacity running.

## Decision

The public alpha does not ship, build, publish, deploy, or support an
in-repository GPU worker image. The image Dockerfile, entrypoint, and publishing
workflow are removed. The old Python entry point remains only as a fail-closed
tombstone returning `EX_UNAVAILABLE` (69), so stale automation receives an
actionable failure instead of a false success.

Pitwall may still broker existing RunPod serverless/public endpoints and may
launch operator-supplied, independently reviewed pod images. Those capabilities
do not imply that the project builds or supports the code inside such images.
Image provenance, model licensing, runtime security, and GPU compatibility are
the operator's responsibility.

## Alternatives considered

- Completing a durable Arq GPU worker was rejected for the first alpha because
  it requires a defined queue protocol, cancellation and retry semantics,
  persistence, resilience tests, and live GPU acceptance evidence.
- Leaving the files labelled experimental was rejected because an executable
  that exits zero can still be mistaken for a healthy worker.

## Consequences

No worker/GPU image is included in the release image matrix or canonical
Compose stack, and live GPU acceptance is not a gate for that removed feature.
Restoring it requires a new ADR, full protocol implementation, locked image
dependencies, security review, unit/integration coverage, and released-digest
GPU acceptance tests.
