-- 0003_workloads.sql
-- State machine widened from (launching/running/completed/failed/killed) to
-- (queued/running/completed/failed/cancelled/timed_out).
-- Cost tracking split into estimate + actual; timing split into execution/queue/cold_start.

CREATE TABLE pitwall.workloads (
  id                       TEXT PRIMARY KEY,
  capability_id            TEXT NOT NULL,
  provider_id              TEXT NOT NULL,
  type                     TEXT NOT NULL,
  state                    TEXT NOT NULL CHECK (state IN
                              ('queued','running','completed','failed','cancelled','timed_out')),
  runpod_job_id            TEXT,
  idempotency_key          TEXT,
  input                    JSONB,
  result                   JSONB,
  fallback_chain           TEXT[],
  error                    JSONB,
  submitted_at             TIMESTAMPTZ NOT NULL,
  started_at               TIMESTAMPTZ,
  completed_at             TIMESTAMPTZ,
  execution_ms             INTEGER,
  queue_ms                 INTEGER,
  cold_start_ms            INTEGER,
  input_bytes              INTEGER,
  output_bytes             INTEGER,
  cost_estimate_usd        NUMERIC(12,6),
  cost_actual_usd          NUMERIC(12,6),
  langfuse_trace_id        TEXT
);

CREATE UNIQUE INDEX idx_workloads_idempotency
  ON pitwall.workloads(idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_workloads_state_submitted
  ON pitwall.workloads(state, submitted_at DESC);

CREATE INDEX idx_workloads_month_spend
  ON pitwall.workloads(submitted_at)
  WHERE state IN ('queued','running','completed');
