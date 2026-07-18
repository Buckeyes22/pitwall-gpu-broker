BEGIN;

DROP SCHEMA IF EXISTS pitwall CASCADE;
\ir ../../db/migrations/0001_capabilities.sql
\ir ../../db/migrations/0002_providers.sql

INSERT INTO pitwall.capabilities (
  id,
  name,
  version,
  class,
  cost_mode,
  config
) VALUES (
  'cap_gpu_lease',
  'GPU Lease',
  'v1',
  'gpu_lease',
  'per_second',
  '{}'::jsonb
);

INSERT INTO pitwall.providers (
  id,
  capability_id,
  name,
  provider_type,
  config,
  priority
) VALUES (
  'prov_canonical_gpu_names',
  'cap_gpu_lease',
  'canonical gpu names',
  'pod_lease',
  '{"gpu_type_priority":["NVIDIA H100 80GB HBM3","NVIDIA L4","NVIDIA GeForce RTX 4090"]}'::jsonb,
  1
);

DO $$
BEGIN
  INSERT INTO pitwall.providers (
    id,
    capability_id,
    name,
    provider_type,
    config,
    priority
  ) VALUES (
    'prov_shorthand_h100',
    'cap_gpu_lease',
    'shorthand h100',
    'pod_lease',
    '{"gpu_type_priority":["H100"]}'::jsonb,
    2
  );
  RAISE EXCEPTION 'expected shorthand GPU name H100 to fail';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.providers (
    id,
    capability_id,
    name,
    provider_type,
    config,
    priority
  ) VALUES (
    'prov_shorthand_l4',
    'cap_gpu_lease',
    'shorthand l4',
    'pod_lease',
    '{"gpu_type_priority":["L4"]}'::jsonb,
    3
  );
  RAISE EXCEPTION 'expected shorthand GPU name L4 to fail';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.providers (
    id,
    capability_id,
    name,
    provider_type,
    config,
    priority
  ) VALUES (
    'prov_shorthand_rtx4090',
    'cap_gpu_lease',
    'shorthand rtx4090',
    'pod_lease',
    '{"gpu_type_priority":["RTX4090"]}'::jsonb,
    4
  );
  RAISE EXCEPTION 'expected shorthand GPU name RTX4090 to fail';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

ROLLBACK;
