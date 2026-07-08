-- 0011_workload_cost_columns.sql
-- Harden workload cost fields.
-- Ensures cost_estimate_usd and cost_actual_usd columns exist on pitwall.workloads,
-- adds non-negative CHECK constraints, and creates the MTD spend partial index
-- used by the advisory-lock budget gate (try_launch, §9.2).
-- Idempotent: uses IF NOT EXISTS so re-running against an existing schema is safe.

ALTER TABLE pitwall.workloads
  ADD COLUMN IF NOT EXISTS cost_estimate_usd NUMERIC(12,6);

ALTER TABLE pitwall.workloads
  ADD COLUMN IF NOT EXISTS cost_actual_usd NUMERIC(12,6);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE connamespace = 'pitwall'::regnamespace
      AND conrelid = 'pitwall.workloads'::regclass
      AND conname = 'workloads_cost_estimate_nonneg'
  ) THEN
    ALTER TABLE pitwall.workloads
      ADD CONSTRAINT workloads_cost_estimate_nonneg
      CHECK (cost_estimate_usd IS NULL OR cost_estimate_usd >= 0);
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE connamespace = 'pitwall'::regnamespace
      AND conrelid = 'pitwall.workloads'::regclass
      AND conname = 'workloads_cost_actual_nonneg'
  ) THEN
    ALTER TABLE pitwall.workloads
      ADD CONSTRAINT workloads_cost_actual_nonneg
      CHECK (cost_actual_usd IS NULL OR cost_actual_usd >= 0);
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_workloads_month_spend
  ON pitwall.workloads(submitted_at)
  WHERE state IN ('queued','running','completed');
