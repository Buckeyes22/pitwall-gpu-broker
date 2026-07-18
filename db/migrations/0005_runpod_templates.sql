-- 0005_runpod_templates.sql
-- Keyed by (name, image_sha) so a new image SHA forces a new template (rollback-
-- friendly) but a repeat launch on the same image hits the cache.
-- §12 metadata columns container_disk_gb, volume_mount_path, env_schema are
-- additive metadata only; env values are never stored here.

CREATE TABLE pitwall.runpod_templates (
  id                       TEXT PRIMARY KEY,
  runpod_template_id       TEXT NOT NULL,
  name                     TEXT NOT NULL,
  image_sha                TEXT NOT NULL,
  image_ref                TEXT NOT NULL,
  registry_auth_id         TEXT,
  container_disk_gb        INTEGER NOT NULL DEFAULT 50,
  volume_mount_path        TEXT NOT NULL DEFAULT '/workspace',
  env_schema               TEXT[],
  created_at               TIMESTAMPTZ DEFAULT now(),
  UNIQUE(name, image_sha)
);

CREATE INDEX idx_runpod_templates_image_sha ON pitwall.runpod_templates(image_sha);
