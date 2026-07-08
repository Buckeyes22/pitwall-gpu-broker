CREATE TABLE pitwall.volumes (
  id                       TEXT PRIMARY KEY,
  runpod_volume_id         TEXT NOT NULL,
  name                     TEXT NOT NULL,
  datacenter_id            TEXT NOT NULL,
  size_gb                  INTEGER NOT NULL CHECK (size_gb > 0),
  purpose                  TEXT,
  equivalent_to            TEXT[],
  sync_strategy            TEXT,
  monthly_cost_usd         NUMERIC(10, 2),
  config                   JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ DEFAULT now(),
  UNIQUE(runpod_volume_id)
);

CREATE INDEX idx_volumes_datacenter
  ON pitwall.volumes(datacenter_id);
