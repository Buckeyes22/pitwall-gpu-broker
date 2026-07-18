CREATE TABLE pitwall.volume_cost_daily (
  day                      DATE NOT NULL,
  volume_id                TEXT NOT NULL REFERENCES pitwall.volumes(id),
  cost_usd                 NUMERIC(12, 6) NOT NULL,
  size_gb                  INTEGER NOT NULL,
  tiered_rate_per_gb      NUMERIC(10, 6) NOT NULL,
  PRIMARY KEY (day, volume_id)
);

CREATE INDEX idx_volume_cost_daily_volume
  ON pitwall.volume_cost_daily(volume_id);

CREATE INDEX idx_volume_cost_daily_day
  ON pitwall.volume_cost_daily(day DESC);
