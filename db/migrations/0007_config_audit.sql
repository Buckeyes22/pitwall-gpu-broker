CREATE TABLE pitwall.config_audit (
  id                       BIGSERIAL PRIMARY KEY,
  actor                    TEXT NOT NULL CHECK (actor IN ('rest:admin','mcp:session-id','system')),
  action                   TEXT NOT NULL CHECK (action IN ('create','update','delete','enable','disable','hibernate')),
  entity_type              TEXT NOT NULL CHECK (entity_type IN ('capability','provider','volume','template')),
  entity_id                TEXT NOT NULL,
  old_value                JSONB,
  new_value                JSONB,
  change_reason            TEXT,
  created_at               TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_audit_entity
  ON pitwall.config_audit(entity_type, entity_id, created_at DESC);
