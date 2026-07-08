-- 0012_alert_events.sql
-- Add alert event state.
-- Persist threshold crossings by UTC month so alert sends are idempotent.
-- The alert_events table tracks which budget thresholds have triggered
-- alerts in each UTC month, preventing duplicate alert sends.

CREATE TABLE pitwall.alert_events (
  month          TEXT NOT NULL,
  threshold_pct  INTEGER NOT NULL,
  sent_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (month, threshold_pct)
);

COMMENT ON TABLE pitwall.alert_events IS
  'Tracks which budget threshold alerts have been sent per UTC month for idempotency.';
COMMENT ON COLUMN pitwall.alert_events.month IS
  'UTC month in YYYY-MM format';
COMMENT ON COLUMN pitwall.alert_events.threshold_pct IS
  'Budget percentage threshold that was crossed (e.g., 50, 75, 90)';
COMMENT ON COLUMN pitwall.alert_events.sent_at IS
  'Timestamp when the alert was sent';
