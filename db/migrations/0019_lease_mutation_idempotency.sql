-- Exactly-once retry semantics for public lease mutations.

-- The original audit constraints only covered administrative configuration
-- changes. Extend them explicitly for lease-domain mutations and the MCP admin
-- actor already used by the public tool surface.
ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_actor_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_actor_check CHECK (
    actor IN ('rest:admin', 'mcp:session-id', 'mcp:admin', 'rest:lease', 'mcp', 'system')
  );

ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_action_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_action_check CHECK (
    action IN ('create', 'update', 'delete', 'enable', 'disable', 'hibernate', 'patch', 'renew')
  );

ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_entity_type_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_entity_type_check CHECK (
    entity_type IN ('capability', 'provider', 'volume', 'template', 'drill', 'lease')
  );

CREATE TABLE pitwall.lease_mutation_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  lease_id       TEXT NOT NULL REFERENCES pitwall.leases(id) ON DELETE CASCADE,
  operation      TEXT NOT NULL CHECK (operation IN ('patch', 'renew')),
  request_hash   TEXT NOT NULL CHECK (length(request_hash) = 64),
  actor          TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_lease_mutation_idempotency_created_at
  ON pitwall.lease_mutation_idempotency(created_at);

COMMENT ON TABLE pitwall.lease_mutation_idempotency IS
  'Deduplicates retried lease PATCH and renewal mutations across REST and MCP.';
