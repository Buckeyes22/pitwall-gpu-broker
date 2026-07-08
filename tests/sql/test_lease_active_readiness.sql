BEGIN;

DROP SCHEMA IF EXISTS pitwall CASCADE;
\ir ../../db/migrations/0001_capabilities.sql
\ir ../../db/migrations/0002_providers.sql
\ir ../../db/migrations/0003_workloads.sql
\ir ../../db/migrations/0004_leases.sql

INSERT INTO pitwall.capabilities (
  id, name, version, class, cost_mode, config
) VALUES (
  'cap_gpu_lease', 'GPU Lease', 'v1', 'gpu_lease', 'per_second', '{}'::jsonb
);

INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority
) VALUES (
  'prov_lease', 'cap_gpu_lease', 'RunPod Lease',
  'pod_lease', '{"gpu_type_priority":["NVIDIA H100 80GB HBM3"]}'::jsonb, 1
);

-- 1. Valid active lease: all three signals present
INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy,
  endpoints, readiness
) VALUES (
  'lse_valid_active', 'prov_lease', 'pod_001', 'active',
  now(), now() + interval '2 hours', 'manual',
  '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
  '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z","probe_method":"ssh_localhost"}'::jsonb
);

-- 2. Non-active states are allowed without readiness signals
INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy
) VALUES
  ('lse_creating',  'prov_lease', 'pod_002', 'creating',         now(), now() + interval '2 hours', 'manual'),
  ('lse_wait_rt',   'prov_lease', 'pod_003', 'waiting_runtime',  now(), now() + interval '2 hours', 'manual'),
  ('lse_wait_probe','prov_lease', 'pod_004', 'waiting_probe',    now(), now() + interval '2 hours', 'manual'),
  ('lse_stopping',  'prov_lease', 'pod_005', 'stopping',         now(), now() + interval '2 hours', 'manual'),
  ('lse_stopped',   'prov_lease', 'pod_006', 'stopped',          now(), now() + interval '2 hours', 'manual'),
  ('lse_failed',    'prov_lease', 'pod_007', 'failed',           now(), now() + interval '2 hours', 'manual'),
  ('lse_expired',   'prov_lease', 'pod_008', 'expired',          now(), now() + interval '2 hours', 'manual');

-- 3. Active lease with NULL endpoints must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_null_endpoints', 'prov_lease', 'pod_bad_1', 'active',
    now(), now() + interval '2 hours', 'manual',
    NULL,
    '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z"}'::jsonb
  );
  RAISE EXCEPTION 'expected active lease with NULL endpoints to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 4. Active lease with NULL readiness must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_null_readiness', 'prov_lease', 'pod_bad_2', 'active',
    now(), now() + interval '2 hours', 'manual',
    '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
    NULL
  );
  RAISE EXCEPTION 'expected active lease with NULL readiness to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 5. Active lease missing runtime_seen_at must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_no_runtime', 'prov_lease', 'pod_bad_3', 'active',
    now(), now() + interval '2 hours', 'manual',
    '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
    '{"port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z"}'::jsonb
  );
  RAISE EXCEPTION 'expected active lease missing runtime_seen_at to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 6. Active lease missing port_mappings_seen_at must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_no_port', 'prov_lease', 'pod_bad_4', 'active',
    now(), now() + interval '2 hours', 'manual',
    '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
    '{"runtime_seen_at":"2026-05-26T14:00:18Z","probe_passed_at":"2026-05-26T14:00:34Z"}'::jsonb
  );
  RAISE EXCEPTION 'expected active lease missing port_mappings_seen_at to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 7. Active lease missing probe_passed_at must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_no_probe', 'prov_lease', 'pod_bad_5', 'active',
    now(), now() + interval '2 hours', 'manual',
    '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
    '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z"}'::jsonb
  );
  RAISE EXCEPTION 'expected active lease missing probe_passed_at to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 8. Active lease with both endpoints and readiness NULL must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_both_null', 'prov_lease', 'pod_bad_6', 'active',
    now(), now() + interval '2 hours', 'manual',
    NULL, NULL
  );
  RAISE EXCEPTION 'expected active lease with both NULL to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 9. Active lease with JSON null readiness timestamp must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_null_runtime_timestamp', 'prov_lease', 'pod_bad_7', 'active',
    now(), now() + interval '2 hours', 'manual',
    '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
    '{"runtime_seen_at":null,"port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z"}'::jsonb
  );
  RAISE EXCEPTION 'expected active lease with JSON null runtime timestamp to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 10. Active lease with blank readiness timestamp must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_active_blank_probe_timestamp', 'prov_lease', 'pod_bad_8', 'active',
    now(), now() + interval '2 hours', 'manual',
    '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
    '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":""}'::jsonb
  );
  RAISE EXCEPTION 'expected active lease with blank probe timestamp to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 11. Verify the valid active lease was actually inserted
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.leases
  WHERE id = 'lse_valid_active' AND state = 'active';
  ASSERT v_count = 1, 'valid active lease was not found';
END
$$;

ROLLBACK;
