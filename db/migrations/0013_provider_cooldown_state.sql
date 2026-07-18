ALTER TABLE pitwall.providers
  ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN cooldown_trips INTEGER NOT NULL DEFAULT 0,
  ADD CONSTRAINT providers_consecutive_failures_non_negative
    CHECK (consecutive_failures >= 0),
  ADD CONSTRAINT providers_cooldown_trips_non_negative
    CHECK (cooldown_trips >= 0);

CREATE INDEX idx_providers_cooldown_until
  ON pitwall.providers(cooldown_until)
  WHERE cooldown_until IS NOT NULL;
