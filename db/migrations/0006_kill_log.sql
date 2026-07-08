CREATE TABLE pitwall.kill_log (
  id                       BIGSERIAL PRIMARY KEY,
  triggered_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  reason                   TEXT NOT NULL,
  actor                    TEXT NOT NULL,
  pods_terminated          INTEGER NOT NULL DEFAULT 0,
  endpoints_hibernated     INTEGER NOT NULL DEFAULT 0,
  workloads_cancelled      INTEGER NOT NULL DEFAULT 0,
  total_duration_ms        INTEGER NOT NULL,
  errors                   JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX idx_kill_log_triggered ON pitwall.kill_log (triggered_at DESC);
