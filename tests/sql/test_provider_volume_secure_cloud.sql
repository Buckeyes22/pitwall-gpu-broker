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

-- Non-volume providers can opt into Community Cloud.
INSERT INTO pitwall.providers (
  id,
  capability_id,
  name,
  provider_type,
  cloud_type,
  config,
  priority
) VALUES (
  'prov_no_volume_community',
  'cap_gpu_lease',
  'no volume community',
  'pod_lease',
  'COMMUNITY',
  '{"gpu_type_priority":["NVIDIA L4"]}'::jsonb,
  1
);

-- Volume-attached providers are valid only on Secure Cloud.
INSERT INTO pitwall.providers (
  id,
  capability_id,
  name,
  provider_type,
  cloud_type,
  config,
  priority
) VALUES (
  'prov_volume_secure',
  'cap_gpu_lease',
  'volume secure',
  'pod_lease',
  'SECURE',
  '{"volume_id":"vol_motorsport_corpus_us_ca","gpu_type_priority":["NVIDIA L4"]}'::jsonb,
  2
);

-- Blank volume ids are treated as no attachment.
INSERT INTO pitwall.providers (
  id,
  capability_id,
  name,
  provider_type,
  cloud_type,
  config,
  priority
) VALUES (
  'prov_blank_volume_community',
  'cap_gpu_lease',
  'blank volume community',
  'pod_lease',
  'COMMUNITY',
  '{"volume_id":"   ","gpu_type_priority":["NVIDIA L4"]}'::jsonb,
  3
);

DO $$
BEGIN
  INSERT INTO pitwall.providers (
    id,
    capability_id,
    name,
    provider_type,
    cloud_type,
    config,
    priority
  ) VALUES (
    'prov_volume_community',
    'cap_gpu_lease',
    'volume community',
    'pod_lease',
    'COMMUNITY',
    '{"volume_id":"vol_motorsport_corpus_us_ca","gpu_type_priority":["NVIDIA L4"]}'::jsonb,
    4
  );
  RAISE EXCEPTION 'expected volume-attached COMMUNITY provider to fail';
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
    cloud_type,
    config,
    priority
  ) VALUES (
    'prov_volume_null_cloud',
    'cap_gpu_lease',
    'volume null cloud',
    'pod_lease',
    NULL,
    '{"volume_id":"vol_motorsport_corpus_us_ca","gpu_type_priority":["NVIDIA L4"]}'::jsonb,
    5
  );
  RAISE EXCEPTION 'expected volume-attached provider with NULL cloud_type to fail';
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
    cloud_type,
    config,
    priority
  ) VALUES (
    'prov_network_volume_community',
    'cap_gpu_lease',
    'network volume community',
    'pod_lease',
    'COMMUNITY',
    '{"networkVolumeId":"rp_nv_abc123","gpu_type_priority":["NVIDIA L4"]}'::jsonb,
    6
  );
  RAISE EXCEPTION 'expected networkVolumeId COMMUNITY provider to fail';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

ROLLBACK;
