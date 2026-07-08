CREATE TABLE pitwall.cost_daily (
  day                      DATE NOT NULL,
  capability_class         TEXT NOT NULL,
  provider_type            TEXT NOT NULL,
  workload_count           INTEGER NOT NULL,
  cost_usd                 NUMERIC(12,6) NOT NULL,
  PRIMARY KEY (day, capability_class, provider_type)
);
