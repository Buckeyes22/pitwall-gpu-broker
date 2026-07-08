-- 0004_leases.sql
-- Lease state machine for pod_lease providers.
-- State lifecycle: creating -> waiting_runtime -> waiting_probe -> active -> stopping -> stopped
-- Terminal states: failed, expired

CREATE FUNCTION pitwall.lease_active_has_readiness_signals(
  lease_state TEXT,
  lease_endpoints JSONB,
  lease_readiness JSONB
) RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
AS $$
  SELECT CASE
    WHEN lease_state <> 'active' THEN true
    WHEN lease_endpoints IS NULL THEN false
    WHEN lease_readiness IS NULL THEN false
    WHEN NULLIF(lease_readiness ->> 'runtime_seen_at', '') IS NULL THEN false
    WHEN NULLIF(lease_readiness ->> 'port_mappings_seen_at', '') IS NULL THEN false
    WHEN NULLIF(lease_readiness ->> 'probe_passed_at', '') IS NULL THEN false
    ELSE true
  END;
$$;

CREATE TABLE pitwall.leases (
  id                       TEXT PRIMARY KEY,
  provider_id              TEXT NOT NULL,
  runpod_pod_id            TEXT NOT NULL,
  state                    TEXT NOT NULL CHECK (state IN
                               ('creating','waiting_runtime','waiting_probe','active','stopping','stopped','failed','expired')),
  created_at               TIMESTAMPTZ NOT NULL,
  expires_at               TIMESTAMPTZ NOT NULL,
  renewal_policy           TEXT NOT NULL,
  endpoints                JSONB,
  readiness                JSONB,
  cost_accrued_usd         NUMERIC(12,6),
  last_health_at           TIMESTAMPTZ,
  terminated_at            TIMESTAMPTZ,
  terminated_reason        TEXT,
  CONSTRAINT leases_expires_after_created CHECK (expires_at > created_at),
  CONSTRAINT leases_active_readiness_signals CHECK (
    pitwall.lease_active_has_readiness_signals(state, endpoints, readiness)
  )
);

CREATE INDEX idx_leases_expires ON pitwall.leases(state, expires_at)
  WHERE state = 'active';
