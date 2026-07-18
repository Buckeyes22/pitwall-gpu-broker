BEGIN;

DROP SCHEMA IF EXISTS pitwall CASCADE;
\ir ../../db/migrations/0001_capabilities.sql
\ir ../../db/migrations/0002_providers.sql
\ir ../../db/migrations/0003_workloads.sql
\ir ../../db/migrations/0004_leases.sql
\ir ../../db/migrations/0005_runpod_templates.sql
\ir ../../db/migrations/0006_kill_log.sql
\ir ../../db/migrations/0007_config_audit.sql
\ir ../../db/migrations/0008_volumes.sql
\ir ../../db/migrations/0009_cost_daily.sql
\ir ../../db/migrations/0010_rate_buckets.sql
\ir ../../db/migrations/0011_workload_cost_columns.sql
\ir ../../db/migrations/0012_alert_events.sql
\ir ../../db/migrations/0013_provider_cooldown_state.sql
\ir ../../db/migrations/0014_async_job_migration.sql

-- Seed a capability + provider so workloads have valid FK targets
INSERT INTO pitwall.capabilities (
  id, name, version, class, cost_mode, config
) VALUES (
  'cap_embed', 'Embedding', 'v1', 'inference', 'per_token', '{}'::jsonb
);

INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority
) VALUES (
  'prov_runpod_bge', 'cap_embed', 'RunPod BGE-M3',
  'serverless_queue', '{"gpu_type_priority":["NVIDIA L4"]}'::jsonb, 1
);

-- ============================================================
-- 1. runpod_webhook_deliveries UNIQUE(runpod_job_id, attempt)
-- ============================================================

-- 1a. Insert a valid delivery row
INSERT INTO pitwall.runpod_webhook_deliveries (runpod_job_id, attempt, payload)
VALUES ('job_abc123', 1, '{"status": "COMPLETED"}'::jsonb);

-- 1b. Duplicate (runpod_job_id, attempt) must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.runpod_webhook_deliveries (runpod_job_id, attempt, payload)
  VALUES ('job_abc123', 1, '{"status": "COMPLETED"}'::jsonb);
  RAISE EXCEPTION 'expected duplicate (runpod_job_id, attempt) to be rejected';
EXCEPTION WHEN unique_violation THEN
  NULL;
END
$$;

-- 1c. Same runpod_job_id with different attempt should succeed
INSERT INTO pitwall.runpod_webhook_deliveries (runpod_job_id, attempt, payload)
VALUES ('job_abc123', 2, '{"status": "COMPLETED"}'::jsonb);

-- 1d. Different runpod_job_id with same attempt should succeed
INSERT INTO pitwall.runpod_webhook_deliveries (runpod_job_id, attempt, payload)
VALUES ('job_def456', 1, '{"status": "IN_PROGRESS"}'::jsonb);

-- 1e. Attempt CHECK constraint: attempt=0 must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.runpod_webhook_deliveries (runpod_job_id, attempt, payload)
  VALUES ('job_chk_attempt', 0, '{}'::jsonb);
  RAISE EXCEPTION 'expected attempt=0 to be rejected by CHECK constraint';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 1f. Attempt CHECK constraint: attempt=4 must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.runpod_webhook_deliveries (runpod_job_id, attempt, payload)
  VALUES ('job_chk_attempt', 4, '{}'::jsonb);
  RAISE EXCEPTION 'expected attempt=4 to be rejected by CHECK constraint';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- ============================================================
-- 2. idempotency_keys PRIMARY KEY (idempotency_key)
-- ============================================================

-- 2a. Insert a valid idempotency key
INSERT INTO pitwall.idempotency_keys (idempotency_key, workload_id)
VALUES ('idem-key-001', 'wkl_001');

-- 2b. Duplicate idempotency_key must be rejected (PK violation)
DO $$
BEGIN
  INSERT INTO pitwall.idempotency_keys (idempotency_key, workload_id)
  VALUES ('idem-key-001', 'wkl_002');
  RAISE EXCEPTION 'expected duplicate idempotency_key to be rejected';
EXCEPTION WHEN unique_violation THEN
  NULL;
END
$$;

-- 2c. Different idempotency_key should succeed
INSERT INTO pitwall.idempotency_keys (idempotency_key, workload_id)
VALUES ('idem-key-002', 'wkl_002');

-- ============================================================
-- 3. workloads idempotency_key partial unique index
-- ============================================================

-- 3a. Insert a workload with an idempotency_key
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at, idempotency_key
) VALUES (
  'wl_idem_320_1', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
  now(), 'wl-idem-320-abc'
);

-- 3b. Duplicate idempotency_key in workloads must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.workloads (
    id, capability_id, provider_id, type, state, submitted_at, idempotency_key
  ) VALUES (
    'wl_idem_320_dup', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
    now(), 'wl-idem-320-abc'
  );
  RAISE EXCEPTION 'expected duplicate idempotency_key in workloads to be rejected';
EXCEPTION WHEN unique_violation THEN
  NULL;
END
$$;

-- 3c. Multiple workloads with NULL idempotency_key are fine
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at
) VALUES
  ('wl_no_idem_320_1', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued', now()),
  ('wl_no_idem_320_2', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued', now());

ROLLBACK;
