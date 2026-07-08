CREATE SCHEMA IF NOT EXISTS pitwall;

CREATE TABLE pitwall.capabilities (
  id                       TEXT PRIMARY KEY,
  name                     TEXT UNIQUE NOT NULL,
  version                  TEXT NOT NULL,
  class                    TEXT NOT NULL,
  cost_mode                TEXT NOT NULL CHECK (cost_mode IN ('per_second','per_request','per_token')),
  config                   JSONB NOT NULL,
  source                   TEXT NOT NULL DEFAULT 'api',
  last_applied_yaml_hash   TEXT,
  enabled                  BOOLEAN DEFAULT true,
  created_at               TIMESTAMPTZ DEFAULT now(),
  updated_at               TIMESTAMPTZ DEFAULT now()
);
