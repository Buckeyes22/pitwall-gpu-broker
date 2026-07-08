# Webhook contract

Pitwall's outbound webhook contract is versioned independently of the REST API.
The current event envelope is version `1`:

```json
{
  "version": "1",
  "event": "workload.completed",
  "delivery_id": "a stable UUID shared by every retry",
  "occurred_at": "2026-07-17T20:00:00Z",
  "workload_id": "wkl_...",
  "consumer": "example-consumer",
  "data": {}
}
```

The body is UTF-8 JSON serialized with sorted keys and compact separators. The
exact transmitted bytes are signed as `HMAC-SHA256("<timestamp>." + body)`.
`X-Pitwall-Signature` contains `t=<unix-seconds>,v1=<hex-digest>`, and
`X-Pitwall-Delivery-ID` matches the envelope. Consumers must verify the raw body
before parsing it, use a constant-time digest comparison, reject timestamps
outside their chosen window, and deduplicate by delivery ID. A standalone
stdlib verifier is in [`examples/verify_webhook.py`](../examples/verify_webhook.py),
with a golden vector in
[`tests/fixtures/webhooks/completion-v1.json`](../tests/fixtures/webhooks/completion-v1.json).

Delivery permits only HTTPS port 443. Pitwall rejects user information,
fragments, ambiguous numeric hosts, and every non-global A/AAAA answer. Each
attempt resolves again, rejects mixed public/private answers, pins the socket to
one validated address, verifies the connected peer, and uses the original host
for TLS SNI. Redirects are never followed. Statuses 408, 425, 429, and 5xx plus
transport timeouts retry at most four times with exponential backoff and jitter;
other non-2xx statuses are terminal.

Subscription management requires the `webhook:admin` bearer scope. Creation
returns a generated signing secret once. List responses omit the secret and URL
query string. Rotation returns a new secret once; deactivate, activate, and
delete operations are audited. Signing secrets use versioned AES-256-GCM at
rest. Configure `PITWALL_WEBHOOK_ENCRYPTION_KEYS` as a JSON map of key version
to URL-safe base64 32-byte key and select the write key with
`PITWALL_WEBHOOK_ENCRYPTION_CURRENT_KEY`. Retain old keys until every
subscription has been rotated.

Inbound RunPod callbacks are a separate surface. Non-loopback binding requires
`PITWALL_WEBHOOK_SECRET`; `PITWALL_WEBHOOK_PREVIOUS_SECRETS` permits
zero-downtime rotation. The receiver enforces JSON content type, a 1 MiB default
streaming body limit, a `120/60s` per-client rate limit, and the signature
timestamp window before JSON parsing. Duplicate RunPod job/attempt deliveries
are acknowledged with 200 and marked `duplicate: true`; this is idempotent
processing, not rejection of every repeated signature.
