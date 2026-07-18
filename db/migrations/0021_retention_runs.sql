-- Auditable, bounded archive/purge runs.

CREATE TABLE pitwall.retention_runs (
  id                  TEXT PRIMARY KEY,
  started_at          TIMESTAMPTZ NOT NULL,
  completed_at        TIMESTAMPTZ NOT NULL,
  cutoff_at           TIMESTAMPTZ NOT NULL,
  mode                TEXT NOT NULL CHECK (mode IN ('archive', 'archive-purge', 'dry-run')),
  archive_path        TEXT,
  manifest_sha256     TEXT,
  workload_count      INTEGER NOT NULL CHECK (workload_count >= 0),
  deleted_count       INTEGER NOT NULL DEFAULT 0 CHECK (deleted_count >= 0),
  key_version         TEXT,
  status              TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
  error_summary       TEXT
);

CREATE INDEX idx_retention_runs_completed_at
  ON pitwall.retention_runs(completed_at DESC);

ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_entity_type_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_entity_type_check CHECK (
    entity_type IN (
      'capability', 'provider', 'volume', 'template', 'drill', 'lease',
      'webhook_subscription', 'retention_run'
    )
  );

ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_action_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_action_check CHECK (
    action IN (
      'create', 'update', 'delete', 'enable', 'disable', 'hibernate',
      'patch', 'renew', 'rotate', 'deactivate', 'activate', 'archive', 'purge'
    )
  );

COMMENT ON TABLE pitwall.retention_runs IS
  'Durable audit of bounded encrypted archive and purge operations.';
