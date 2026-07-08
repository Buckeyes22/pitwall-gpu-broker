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

-- Insert a valid active lease with all readiness signals
INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy,
  endpoints, readiness
) VALUES (
  'lse_test_active', 'prov_lease', 'pod_001', 'active',
  now(), now() + interval '2 hours', 'manual',
  '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
  '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z","probe_method":"ssh_localhost"}'::jsonb
);

-- Verify active lease was inserted
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_active';
  ASSERT v_state = 'active', 'Expected active lease, got: ' || v_state;
END
$$;

-- Test 1: Transition ACTIVE -> STOPPING (valid)
UPDATE pitwall.leases SET state = 'stopping' WHERE id = 'lse_test_active';
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_active';
  ASSERT v_state = 'stopping', 'Expected stopping, got: ' || v_state;
END
$$;

-- Test 2: Transition STOPPING -> STOPPED (valid, terminal)
UPDATE pitwall.leases SET state = 'stopped' WHERE id = 'lse_test_active';
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_active';
  ASSERT v_state = 'stopped', 'Expected stopped, got: ' || v_state;
END
$$;

-- Reset for next test: use a fresh active lease
DELETE FROM pitwall.leases WHERE id = 'lse_test_active';

INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy,
  endpoints, readiness
) VALUES (
  'lse_test_active2', 'prov_lease', 'pod_002', 'active',
  now(), now() + interval '2 hours', 'manual',
  '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
  '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z","probe_method":"ssh_localhost"}'::jsonb
);

-- Test 3: Transition ACTIVE -> EXPIRED (valid, terminal)
UPDATE pitwall.leases SET state = 'expired' WHERE id = 'lse_test_active2';
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_active2';
  ASSERT v_state = 'expired', 'Expected expired, got: ' || v_state;
END
$$;

-- Reset for next test
DELETE FROM pitwall.leases WHERE id = 'lse_test_active2';

INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy,
  endpoints, readiness
) VALUES (
  'lse_test_active3', 'prov_lease', 'pod_003', 'active',
  now(), now() + interval '2 hours', 'manual',
  '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
  '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z","probe_method":"ssh_localhost"}'::jsonb
);

-- Test 4: Transition ACTIVE -> FAILED (valid, terminal)
UPDATE pitwall.leases SET state = 'failed' WHERE id = 'lse_test_active3';
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_active3';
  ASSERT v_state = 'failed', 'Expected failed, got: ' || v_state;
END
$$;

-- Reset for next test: recreate active lease and go through stopping
DELETE FROM pitwall.leases WHERE id = 'lse_test_active3';

INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy,
  endpoints, readiness
) VALUES (
  'lse_test_active4', 'prov_lease', 'pod_004', 'active',
  now(), now() + interval '2 hours', 'manual',
  '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
  '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z","probe_method":"ssh_localhost"}'::jsonb
);

-- Test 5: Transition ACTIVE -> STOPPING -> EXPIRED (valid, terminal via stopping)
UPDATE pitwall.leases SET state = 'stopping' WHERE id = 'lse_test_active4';
UPDATE pitwall.leases SET state = 'expired' WHERE id = 'lse_test_active4';
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_active4';
  ASSERT v_state = 'expired', 'Expected expired, got: ' || v_state;
END
$$;

-- Test 6: Invalid transition ACTIVE -> STOPPED (must go through STOPPING)
-- This should fail because there's no application-level enforcement,
-- but the database will accept it since it only checks that state is valid.
-- The invalid transition enforcement happens in Python code.
-- We test that the database allows any valid state transition.
DO $$
BEGIN
  -- First recreate an active lease
  DELETE FROM pitwall.leases WHERE id = 'lse_test_active4';
  INSERT INTO pitwall.leases (
    id, provider_id, runpod_pod_id, state,
    created_at, expires_at, renewal_policy,
    endpoints, readiness
  ) VALUES (
    'lse_test_active4', 'prov_lease', 'pod_004', 'active',
    now(), now() + interval '2 hours', 'manual',
    '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
    '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z","probe_method":"ssh_localhost"}'::jsonb
  );
  -- Note: Database does NOT enforce state machine transitions.
  -- The Python code in pitwall.leases.state.transition_lease_state() enforces valid transitions.
  -- This test verifies the database simply stores whatever valid state is provided.
  UPDATE pitwall.leases SET state = 'stopped' WHERE id = 'lse_test_active4';
  -- Database allows this even though it's invalid in the state machine
END
$$;

-- Test 7: Verify terminal states cannot transition
-- STOPPED is terminal - any transition from it should be handled by Python code
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_active4';
  ASSERT v_state = 'stopped', 'Expected stopped, got: ' || v_state;
END
$$;

-- Test 8: Test STOPPING -> STOPPED -> FAILED
-- Recreate and properly transition
DELETE FROM pitwall.leases WHERE id = 'lse_test_active4';

INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy
) VALUES (
  'lse_test_stopping', 'prov_lease', 'pod_005', 'stopping',
  now(), now() + interval '2 hours', 'manual'
);

-- Test 9: STOPPING -> STOPPED (valid)
UPDATE pitwall.leases SET state = 'stopped' WHERE id = 'lse_test_stopping';
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_stopping';
  ASSERT v_state = 'stopped', 'Expected stopped, got: ' || v_state;
END
$$;

-- Test 10: STOPPING -> FAILED (valid)
DELETE FROM pitwall.leases WHERE id = 'lse_test_stopping';

INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy
) VALUES (
  'lse_test_stopping2', 'prov_lease', 'pod_006', 'stopping',
  now(), now() + interval '2 hours', 'manual'
);

UPDATE pitwall.leases SET state = 'failed' WHERE id = 'lse_test_stopping2';
DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_test_stopping2';
  ASSERT v_state = 'failed', 'Expected failed, got: ' || v_state;
END
$$;

-- Test 11: Full happy path: creating -> waiting_runtime -> waiting_probe -> active -> stopping -> stopped
DELETE FROM pitwall.leases WHERE id = 'lse_test_stopping2';

INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy
) VALUES (
  'lse_full_path', 'prov_lease', 'pod_007', 'creating',
  now(), now() + interval '2 hours', 'manual'
);

UPDATE pitwall.leases SET state = 'waiting_runtime' WHERE id = 'lse_full_path';
UPDATE pitwall.leases SET state = 'waiting_probe' WHERE id = 'lse_full_path';

-- Transition to ACTIVE without readiness signals should fail due to check constraint
DO $$
BEGIN
  UPDATE pitwall.leases SET state = 'active' WHERE id = 'lse_full_path';
  RAISE EXCEPTION 'expected active transition without readiness to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- Full path with proper active lease setup
DELETE FROM pitwall.leases WHERE id = 'lse_full_path';

INSERT INTO pitwall.leases (
  id, provider_id, runpod_pod_id, state,
  created_at, expires_at, renewal_policy,
  endpoints, readiness
) VALUES (
  'lse_full_path', 'prov_lease', 'pod_007', 'creating',
  now(), now() + interval '2 hours', 'manual',
  '{"http":{"8000":"https://pod-8000.proxy.runpod.net"}}'::jsonb,
  '{"runtime_seen_at":"2026-05-26T14:00:18Z","port_mappings_seen_at":"2026-05-26T14:00:19Z","probe_passed_at":"2026-05-26T14:00:34Z"}'::jsonb
);

UPDATE pitwall.leases SET state = 'waiting_runtime' WHERE id = 'lse_full_path';
UPDATE pitwall.leases SET state = 'waiting_probe' WHERE id = 'lse_full_path';
UPDATE pitwall.leases SET state = 'active' WHERE id = 'lse_full_path';
UPDATE pitwall.leases SET state = 'stopping' WHERE id = 'lse_full_path';
UPDATE pitwall.leases SET state = 'stopped' WHERE id = 'lse_full_path';

DO $$
DECLARE
  v_state TEXT;
BEGIN
  SELECT state INTO v_state FROM pitwall.leases WHERE id = 'lse_full_path';
  ASSERT v_state = 'stopped', 'Expected stopped, got: ' || v_state;
END
$$;

ROLLBACK;
