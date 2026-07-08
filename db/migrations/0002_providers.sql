CREATE FUNCTION pitwall.provider_gpu_type_priority_is_canonical(gpu_types JSONB)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
  SELECT CASE
    WHEN jsonb_typeof(gpu_types) <> 'array' THEN false
    ELSE NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(gpu_types) AS gpu_type(value)
      WHERE jsonb_typeof(gpu_type.value) <> 'string'
        OR gpu_type.value #>> '{}' NOT IN (
          'NVIDIA H100 80GB HBM3',
          'NVIDIA H100 NVL',
          'NVIDIA H200',
          'NVIDIA H200 NVL',
          'NVIDIA B200',
          'NVIDIA A100 80GB',
          'NVIDIA A100 80GB PCIe',
          'NVIDIA A100 40GB',
          'NVIDIA A6000',
          'NVIDIA RTX A6000',
          'NVIDIA A40',
          'NVIDIA L40',
          'NVIDIA L40S',
          'NVIDIA L4',
          'NVIDIA RTX 6000 Ada',
          'NVIDIA RTX 4090',
          'NVIDIA GeForce RTX 4090',
          'NVIDIA RTX A5000',
          'NVIDIA RTX A4500',
          'NVIDIA RTX A4000',
          'NVIDIA RTX 5000 Ada Generation',
          'NVIDIA RTX 4000 Ada Generation'
        )
    )
  END;
$$;

CREATE TABLE pitwall.providers (
  id                       TEXT PRIMARY KEY,
  capability_id            TEXT REFERENCES pitwall.capabilities(id),
  name                     TEXT NOT NULL,
  provider_type            TEXT NOT NULL CHECK (provider_type IN
                              ('serverless_queue','serverless_lb','public_endpoint','pod_lease')),
  runpod_endpoint_id       TEXT,
  runpod_template_id       TEXT,
  region                   TEXT,
  cloud_type               TEXT CHECK (cloud_type IN ('SECURE','COMMUNITY')),
  config                   JSONB NOT NULL,
  priority                 INTEGER NOT NULL,
  enabled                  BOOLEAN DEFAULT true,
  health_status            TEXT DEFAULT 'unknown',
  cold_start_p50_ms        INTEGER,
  cold_start_p95_ms        INTEGER,
  recent_error_rate        REAL DEFAULT 0,
  cooldown_until           TIMESTAMPTZ,
  source                   TEXT NOT NULL DEFAULT 'api',
  last_applied_yaml_hash   TEXT,
  updated_at               TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT providers_volume_requires_secure_cloud CHECK (
    (
      NULLIF(BTRIM(config ->> 'volume_id'), '') IS NULL
      AND NULLIF(BTRIM(config ->> 'networkVolumeId'), '') IS NULL
    )
    OR cloud_type IS NOT DISTINCT FROM 'SECURE'
  ),
  CONSTRAINT providers_gpu_type_priority_canonical CHECK (
    NOT (config ? 'gpu_type_priority')
    OR pitwall.provider_gpu_type_priority_is_canonical(config -> 'gpu_type_priority') IS TRUE
  )
);

CREATE INDEX idx_providers_capability_priority
  ON pitwall.providers(capability_id, priority)
  WHERE enabled = true;
