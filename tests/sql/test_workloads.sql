BEGIN;

DROP SCHEMA IF EXISTS pitwall CASCADE;
\ir ../../db/migrations/0001_capabilities.sql
\ir ../../db/migrations/0002_providers.sql
\ir ../../db/migrations/0003_workloads.sql

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

-- 1. Insert a valid workload in 'queued' state
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_estimate_usd
) VALUES (
  'wl_001', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
  now(), 0.001250
);

-- 2. Transition to running (set started_at, cold_start_ms)
UPDATE pitwall.workloads
SET state = 'running', started_at = now(), cold_start_ms = 3200
WHERE id = 'wl_001';

-- 3. Transition to completed (set completed_at, execution_ms, cost_actual_usd, result)
UPDATE pitwall.workloads
SET state = 'completed', completed_at = now(),
    execution_ms = 5400, queue_ms = 120,
    cost_actual_usd = 0.000890,
    result = '{"embedding": [0.1, 0.2, 0.3]}'::jsonb,
    input_bytes = 256, output_bytes = 12288
WHERE id = 'wl_001';

-- 4. Verify all 6 states are accepted
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at
) VALUES
  ('wl_queued',    'cap_embed', 'prov_runpod_bge', 'inference', 'queued',     now()),
  ('wl_running',   'cap_embed', 'prov_runpod_bge', 'inference', 'running',    now()),
  ('wl_completed', 'cap_embed', 'prov_runpod_bge', 'inference', 'completed',  now()),
  ('wl_failed',    'cap_embed', 'prov_runpod_bge', 'inference', 'failed',     now()),
  ('wl_cancelled', 'cap_embed', 'prov_runpod_bge', 'inference', 'cancelled',  now()),
  ('wl_timed_out', 'cap_embed', 'prov_runpod_bge', 'inference', 'timed_out',  now());

-- 5. Invalid state must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.workloads (
    id, capability_id, provider_id, type, state, submitted_at
  ) VALUES (
    'wl_bad_state', 'cap_embed', 'prov_runpod_bge', 'inference', 'launching', now()
  );
  RAISE EXCEPTION 'expected invalid state to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 6. Idempotency key uniqueness (partial unique index, NULLs allowed)
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at, idempotency_key
) VALUES (
  'wl_idem_1', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
  now(), 'idem-abc'
);

DO $$
BEGIN
  INSERT INTO pitwall.workloads (
    id, capability_id, provider_id, type, state, submitted_at, idempotency_key
  ) VALUES (
    'wl_idem_dup', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
    now(), 'idem-abc'
  );
  RAISE EXCEPTION 'expected duplicate idempotency_key to be rejected';
EXCEPTION WHEN unique_violation THEN
  NULL;
END
$$;

-- 7. Multiple rows with NULL idempotency_key should be fine
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at
) VALUES
  ('wl_no_idem_1', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued', now()),
  ('wl_no_idem_2', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued', now());

-- 8. Cost precision: NUMERIC(12,6) should accept and store micro-dollar precision
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_estimate_usd, cost_actual_usd
) VALUES (
  'wl_cost_prec', 'cap_embed', 'prov_runpod_bge', 'inference', 'completed',
  now(), 123456.789012, 0.000001
);

-- 9. Verify cost round-trip
DO $$
DECLARE
  v_est NUMERIC;
  v_act NUMERIC;
BEGIN
  SELECT cost_estimate_usd, cost_actual_usd INTO v_est, v_act
  FROM pitwall.workloads WHERE id = 'wl_cost_prec';
  ASSERT v_est = 123456.789012, 'cost_estimate_usd did not round-trip';
  ASSERT v_act = 0.000001, 'cost_actual_usd did not round-trip';
END
$$;

-- 10. Fallback_chain (TEXT[]) and error (JSONB) can store structured data
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at,
  fallback_chain, error
) VALUES (
  'wl_fallback', 'cap_embed', 'prov_runpod_bge', 'inference', 'failed',
  now(),
  ARRAY['prov_runpod_bge', 'prov_backup_a', 'prov_backup_b']::TEXT[],
  '{"code":"capacity_exhausted","message":"no GPUs available","retries":3}'::jsonb
);

ROLLBACK;
