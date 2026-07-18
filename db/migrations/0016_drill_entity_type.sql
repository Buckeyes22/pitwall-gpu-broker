-- 0016_drill_entity_type.sql
-- Persist drill evidence
-- Adds 'drill' to the config_audit entity_type CHECK constraint so drill
-- evidence can be persisted as a config_audit row alongside the JSON report.

ALTER TABLE pitwall.config_audit
    DROP CONSTRAINT IF EXISTS config_audit_entity_type_check;

ALTER TABLE pitwall.config_audit
    ADD CONSTRAINT config_audit_entity_type_check
    CHECK (entity_type IN ('capability', 'provider', 'volume', 'template', 'drill'));
