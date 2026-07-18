CREATE TABLE pitwall.rate_buckets (
  endpoint_id              TEXT NOT NULL,
  operation                TEXT NOT NULL,
  capacity                 INTEGER NOT NULL CHECK (capacity > 0),
  tokens                   REAL NOT NULL CHECK (tokens >= 0),
  last_refilled_at         TIMESTAMPTZ NOT NULL,
  recent_429_at            TIMESTAMPTZ,
  PRIMARY KEY (endpoint_id, operation)
);
