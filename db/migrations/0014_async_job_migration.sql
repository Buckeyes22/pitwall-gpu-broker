-- 0014_async_job_migration.sql
-- Create async job migration.
-- Adds tables for idempotency key storage, RunPod webhook delivery tracking,
-- and consumer webhook subscriptions.
--
-- Idempotency: Keys stored for 24 hours, GC'd nightly via Arq.
-- Webhook receiver: dedupe by (runpod_job_id, attempt), fast-200, replay-safe.
-- Consumer webhooks: HMAC-signed delivery with retry semantics.

-- 1. idempotency_keys: maps idempotency_key -> workload_id with 24h TTL
CREATE TABLE pitwall.idempotency_keys (
    idempotency_key     TEXT PRIMARY KEY,
    workload_id         TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_idempotency_keys_created_at
    ON pitwall.idempotency_keys(created_at);

COMMENT ON TABLE pitwall.idempotency_keys IS
    'Stores Idempotency-Key to workload_id mappings. GCd nightly after 24 hours.';
COMMENT ON COLUMN pitwall.idempotency_keys.idempotency_key IS
    'Client-supplied Idempotency-Key header value.';
COMMENT ON COLUMN pitwall.idempotency_keys.workload_id IS
    'The workload_id that was created/returned for this idempotency key.';
COMMENT ON COLUMN pitwall.idempotency_keys.created_at IS
    'Timestamp when the idempotency key was first used.';

-- 2. runpod_webhook_deliveries: tracks RunPod webhook deliveries for deduplication
-- RunPod retries 2 times with 10s delay, so attempt ranges from 1-3
CREATE TABLE pitwall.runpod_webhook_deliveries (
    id                 BIGSERIAL PRIMARY KEY,
    runpod_job_id      TEXT NOT NULL,
    attempt            INTEGER NOT NULL CHECK (attempt >= 1 AND attempt <= 3),
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload            JSONB,
    UNIQUE (runpod_job_id, attempt)
);

CREATE INDEX idx_runpod_webhook_deliveries_received_at
    ON pitwall.runpod_webhook_deliveries(received_at);

COMMENT ON TABLE pitwall.runpod_webhook_deliveries IS
    'Tracks RunPod webhook deliveries for idempotent deduplication. Dedup key is (runpod_job_id, attempt).';
COMMENT ON COLUMN pitwall.runpod_webhook_deliveries.runpod_job_id IS
    'RunPod job ID from the webhook payload.';
COMMENT ON COLUMN pitwall.runpod_webhook_deliveries.attempt IS
    'Delivery attempt number (1-3, RunPod retries 2 times).';
COMMENT ON COLUMN pitwall.runpod_webhook_deliveries.received_at IS
    'Timestamp when the webhook was received.';
COMMENT ON COLUMN pitwall.runpod_webhook_deliveries.payload IS
    'Raw webhook payload JSON for async processing.';

-- 3. webhook_subscriptions: consumer-registered webhook URLs for result callbacks
CREATE TABLE pitwall.webhook_subscriptions (
    id                 BIGSERIAL PRIMARY KEY,
    consumer           TEXT NOT NULL,
    webhook_url        TEXT NOT NULL,
    hmac_secret        TEXT,
    active             BOOLEAN NOT NULL DEFAULT true,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_webhook_subscriptions_consumer
    ON pitwall.webhook_subscriptions(consumer, active);

COMMENT ON TABLE pitwall.webhook_subscriptions IS
    'Consumer-registered webhook URLs for async job result callbacks.';
COMMENT ON COLUMN pitwall.webhook_subscriptions.consumer IS
    'Consumer identifier (e.g., consumer name or ID).';
COMMENT ON COLUMN pitwall.webhook_subscriptions.webhook_url IS
    'URL to POST job results to when complete.';
COMMENT ON COLUMN pitwall.webhook_subscriptions.hmac_secret IS
    'Secret for HMAC-signed webhook delivery (NULL means no signing).';
COMMENT ON COLUMN pitwall.webhook_subscriptions.active IS
    'Whether this subscription is currently active.';
COMMENT ON COLUMN pitwall.webhook_subscriptions.created_at IS
    'Timestamp when the subscription was created.';
COMMENT ON COLUMN pitwall.webhook_subscriptions.updated_at IS
    'Timestamp when the subscription was last updated.';
