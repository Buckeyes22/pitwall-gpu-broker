-- 0018_lease_auto_teardown.sql
-- Add the missing pitwall.leases.auto_teardown_on_expiry column.
-- The column is required by code that predates this migration:
--   * LeaseRepository.create INSERTs it (db/repository.py) — lease creation
--     fails with UndefinedColumnError without it.
--   * The lease-expiry reconciler query (_LEASE_EXPIRY_LEASES_SQL) selects it
--     and filters `WHERE auto_teardown_on_expiry = true` — expired-lease
--     auto-teardown crashes without it.
-- The Lease model, lease API schemas, MCP tool, and route handlers all carry the
-- field with a default of true; this migration brings the schema in line.
-- Idempotent: uses IF NOT EXISTS so re-running against an existing schema is safe.

ALTER TABLE pitwall.leases
  ADD COLUMN IF NOT EXISTS auto_teardown_on_expiry BOOLEAN NOT NULL DEFAULT true;
