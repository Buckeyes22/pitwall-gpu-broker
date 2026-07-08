# Operator data handling

Pitwall is self-hosted. The operator chooses inputs, providers, storage, regions,
retention, and optional integrations. This document is an engineering inventory,
not legal advice.

| Data | Primary location | Default lifecycle | Possible egress |
| --- | --- | --- | --- |
| Capability/provider configuration | Postgres | Until changed or deleted | RunPod identifiers are used in provider calls |
| Inference inputs and outputs | Workload records/Postgres; provider | Subject to operator workflow and retention mode | RunPod; optional Langfuse metadata |
| Lease, job, cost, and audit state | Postgres | Active records plus configured retention | RunPod lifecycle APIs; optional alerts |
| Queue/idempotency/rate-limit state | Redis | Operational TTL or Redis persistence | None beyond the deployment |
| Inbound webhook bodies | Postgres/queue as required by workflow | Deduplicated and retention-managed | None by default |
| Outbound webhook metadata | Postgres | Until deactivated/deleted | Operator-approved HTTPS destination |
| Webhook signing secrets | Postgres, AES-256-GCM encrypted | Until rotation/deletion | Used only to sign configured delivery |
| Archives/backups | Operator filesystem/object store | Operator policy | Configured S3/R2 backup destination |
| Logs and metrics | Process output/Prometheus consumer | Operator policy | Optional log/metrics collector |

The encrypted retention job supports dry-run, bounded archive, purge, and an
audited run record. `archive-purge` removes eligible database records only after
the encrypted archive is durably written; related object deletion is confined
to configured roots. Operators must retain old encryption keys as long as the
corresponding archives or encrypted webhook secrets exist.

Public health endpoints expose only component availability. Central logging
redaction removes configured credentials, authenticated URLs, authorization
headers, labeled secrets, and bearer tokens. Operators should still avoid
placing personal or regulated data in identifiers, tags, exception text, or
provider metadata.

To answer an access/export/delete request, operators must search their own
Postgres, Redis persistence, archives, backups, provider account, webhook
destinations, and optional integrations. The project has no remote copy and
cannot perform this operation for an installation.
