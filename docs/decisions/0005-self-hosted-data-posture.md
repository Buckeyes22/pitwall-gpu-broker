# ADR 0005: Self-hosted data and privacy posture

- Status: Accepted for the public alpha
- Date: 2026-07-17
- Decision owner: Project lead and privacy reviewer

## Decision

The project provides self-hosted software only. It operates no SaaS control
plane, hosted telemetry collector, documentation analytics, package mirror, or
managed database. Project maintainers do not receive deployment data merely
because an operator installs Pitwall.

The deploying operator is responsible for deciding its legal role and for the
data processed through RunPod, Postgres, Redis, webhook destinations, object
storage, and any optional observability service. Pitwall cannot determine
whether that operator is a controller, processor, or neither in a particular
jurisdiction.

Langfuse tracing is disabled unless the operator installs the tracing extra and
sets its credentials. R2/S3 log staging, Resend alerts, outbound webhooks, and
RunPod calls are likewise explicit operator-configured egress. No anonymous
usage or crash telemetry is emitted.

## Consequences

The repository must maintain a data inventory, egress map, retention controls,
and redaction policy. Adding project-operated collection or analytics requires
a new ADR, privacy/legal review, a public notice, a deletion/export process, and
an opt-in migration plan.
