-- 0015_webhook_delivery_failures.sql
-- Add dispatcher
-- Adds table for tracking consumer webhook delivery failures separately from workload state.
-- Bounded retries: max 4 attempts (0s initial + 3 retries with 1, 3, 9s delays).
-- Failures are recorded separately so workload state is not polluted by delivery failures.

CREATE TABLE pitwall.webhook_delivery_failures (
    id                     BIGSERIAL PRIMARY KEY,
    workload_id            TEXT NOT NULL,
    subscription_id        BIGINT NOT NULL REFERENCES pitwall.webhook_subscriptions(id),
    attempt                INTEGER NOT NULL CHECK (attempt >= 1 AND attempt <= 4),
    attempted_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at         TIMESTAMPTZ,
    payload                JSONB NOT NULL,
    status_code            INTEGER,
    error_message          TEXT,
    UNIQUE (workload_id, subscription_id, attempt)
);

CREATE INDEX idx_webhook_delivery_failures_workload_id
    ON pitwall.webhook_delivery_failures(workload_id);

CREATE INDEX idx_webhook_delivery_failures_subscription_id
    ON pitwall.webhook_delivery_failures(subscription_id);

CREATE INDEX idx_webhook_delivery_failures_next_retry_at
    ON pitwall.webhook_delivery_failures(next_retry_at);

COMMENT ON TABLE pitwall.webhook_delivery_failures IS
    'Tracks consumer webhook delivery failures separately from workload state. '
    'Max 4 delivery attempts (initial + 3 retries) with exponential backoff.';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.workload_id IS
    'The workload ID this delivery attempt is for.';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.subscription_id IS
    'The webhook subscription ID this delivery was attempted for.';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.attempt IS
    'Delivery attempt number (1-4, bounded retries with exponential backoff).';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.attempted_at IS
    'Timestamp when this delivery attempt was made.';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.next_retry_at IS
    'Timestamp for next retry attempt, NULL if no more retries.';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.payload IS
    'The webhook payload that was attempted to be delivered.';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.status_code IS
    'HTTP status code received from the webhook endpoint.';
COMMENT ON COLUMN pitwall.webhook_delivery_failures.error_message IS
    'Error message if the delivery failed.';