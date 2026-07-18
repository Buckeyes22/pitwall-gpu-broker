# Incident Response Runbook

This runbook covers the supported single-operator public-alpha deployment. Protect customer
payloads, database URLs, bearer tokens, webhook secrets, archive keys, and RunPod identifiers in
all evidence. Use GitHub Private Vulnerability Reporting for a suspected product vulnerability;
do not attach sensitive material to a public issue.

## Immediate spend containment

For suspected runaway spend or credential compromise, invoke the authenticated account-wide kill
switch. When API bearer auth is configured, both gates are required:

```bash
curl --fail-with-body --max-time 60 -X POST \
  -H "Authorization: Bearer ${PITWALL_API_TOKEN}" \
  -H "X-Pitwall-Secret: ${PITWALL_ADMIN_SECRET}" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"operator incident containment","terminate_compute":true}' \
  http://127.0.0.1:8080/v1/admin/kill-switch
```

The ordered stages are network deny, matching-device revocation, then RunPod compute termination.
The target is under 30 seconds, but the response is authoritative: inspect `errors`,
`tailscale_acl_updated`, `devices_removed`, and `pods_terminated`. A no-op network adapter does not
prove isolation. Confirm in the RunPod and, when configured, Tailscale control planes that no
matching resources remain. The activation is recorded in `pitwall.kill_log`.

There is no “resume” kill-switch operation. Restore credentials and dependencies, correct the
trigger, verify readiness, then restart normal services deliberately.

## Compromised credential

1. Contain spend with the kill switch when the RunPod or control-plane credential may be exposed.
2. Revoke the credential at its issuer before investigating from potentially compromised hosts.
3. Rotate API bearer tokens, admin secret, inbound webhook secrets, database/Redis passwords, and
   encryption keys according to the affected scope.
4. For outbound webhook secret-encryption rotation, keep old key versions until every stored secret
   has been rotated and backup recovery no longer requires them.
5. Search redacted logs and audit rows for use after the suspected exposure time.
6. Replace the deployment from reviewed immutable artifacts; do not merely edit a secret in a
   running compromised container.

## Runaway spend or orphaned resources

1. Invoke the kill switch.
2. Independently inventory RunPod pods, endpoints, and volumes in the provider console/API.
3. Compare active Pitwall leases and queued/running workloads with that inventory.
4. Terminate orphaned billable resources at the provider, recording redacted evidence.
5. Inspect budget admission, estimated versus actual cost, retries, and idempotency keys for the
   originating workload.
6. Keep dispatch stopped until provider inventory is empty or every remaining resource has an
   approved owner and cap.

## PostgreSQL unavailable, lost, or failed migration

1. Stop API/reconciler/webhook writes; do not repeatedly rerun an unknown failed migration.
2. Preserve database/server logs and `pitwall-gpu-broker db status` output with credentials redacted.
3. If checksum drift is reported, restore the original applied migration file; never modify the
   recorded checksum or edit production schema ad hoc to silence the guard.
4. For data loss or an incompatible migration, restore the pre-upgrade backup into a new database.
5. Run the complete `pg_dump`/`pg_restore` drill and compare every source/restore base table.
6. Point the prior compatible application digest at the validated database, verify `/readyz`, then
   re-enable traffic.

Migrations are forward-only unless release notes explicitly provide a tested reverse path. See
`upgrade-recovery.md`.

## Redis outage or queue backlog

1. Stop new spend-capable submissions if the queue or deduplication state cannot be trusted.
2. Confirm Redis authentication, capacity, persistence, and latency from the private network.
3. Inspect queue depth, oldest queued/running workload age, worker health, and retry counts.
4. Do not flush Redis during an incident unless the loss of queued/idempotency state is understood
   and approved.
5. Restore Redis, restart one reconciler, and observe bounded drain before increasing concurrency.
6. Reconcile queued/running database rows against RunPod so replay cannot duplicate spend.

## Webhook abuse or delivery storm

For inbound abuse:

1. Preserve rate-limit and signature-failure counts without retaining raw hostile payloads.
2. Rotate `PITWALL_WEBHOOK_SECRET` using `PITWALL_WEBHOOK_PREVIOUS_SECRETS` for the bounded overlap
   needed by a coordinated rotation.
3. Tighten the receiver network boundary and rate limit; do not disable HMAC.
4. Verify body-size, content-type, timestamp, replay-window, and duplicate-delivery behavior.

For outbound failures:

1. Inspect due and terminal failure metrics plus safe subscription metadata.
2. Deactivate the affected subscription when a destination is failing or suspected compromised.
3. Do not weaken SSRF controls or enable redirects to make a destination work.
4. Rotate the write-only signing secret and reactivate only after the receiver verifies a golden
   signed fixture.

## Provider outage or timeout storm

1. Check the provider's status and Pitwall unhealthy-provider/reconciliation metrics.
2. Keep retries bounded; avoid manual loops that bypass idempotency or budget admission.
3. Cancel or hold queued work if delay is safer than fallback. The public alpha supports RunPod
   only; it does not promise a second cloud or homelab failover.
4. After recovery, reconcile each nonterminal workload and verify orphan cleanup before resuming
   normal throughput.

## Bad release

1. Stop rollout and set `PITWALL_RELEASE_ENABLED` false for subsequent workflow attempts.
2. Do not replace or delete published bytes. Yank the Python version when appropriate and deprecate
   the image tag/digest.
3. For application-only rollback, deploy the prior immutable digest only when schema compatibility
   permits it.
4. If an incompatible migration ran, restore the pre-upgrade backup to a new database and run the
   prior version against that restored target.
5. Revoke any compromised publishing identity, issue a corrected version, update the advisory and
   release notes, and record which external channels received the correction.

## Evidence collection

Collect the minimum evidence needed to establish timeline and impact:

- UTC start/detection/containment/recovery times;
- release version, image digests, Compose config hash, and migration status;
- redacted kill report, audit IDs, workload/lease IDs, and provider resource counts;
- readiness and Prometheus metrics around the incident window;
- bounded, redacted service logs and provider error classes;
- credential rotations/revocations and resource-cleanup confirmations;
- backup/restore evidence when data or migrations are involved.

Never collect full environment dumps, raw authorization headers, complete database/Redis URLs,
webhook signing secrets, archive keys, or customer inputs/results by default. Store incident
evidence in the operator's access-controlled system, not in this repository.

## Closure

Before closing an incident:

- verify no unowned RunPod resources remain;
- verify `/readyz`, queue drain, webhook delivery, and cost metrics;
- add a regression test for any product defect;
- document corrective actions, owners, and deadlines;
- update this runbook when the response exposed a missing or unsafe step;
- determine whether a private vulnerability report or public advisory is required.
