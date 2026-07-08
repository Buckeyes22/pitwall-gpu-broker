-- Encrypt outbound webhook secrets and complete the management audit surface.

ALTER TABLE pitwall.webhook_subscriptions
  ADD COLUMN hmac_secret_ciphertext BYTEA,
  ADD COLUMN hmac_secret_nonce BYTEA,
  ADD COLUMN hmac_secret_key_version TEXT;

-- Pre-release plaintext secrets cannot be safely transformed without runtime
-- key material. Deactivate legacy rows before removing plaintext; operators
-- must rotate each subscription through the authenticated API.
UPDATE pitwall.webhook_subscriptions SET active = false;
ALTER TABLE pitwall.webhook_subscriptions DROP COLUMN hmac_secret;

ALTER TABLE pitwall.webhook_subscriptions
  ADD CONSTRAINT webhook_active_has_encrypted_secret CHECK (
    NOT active OR (
      hmac_secret_ciphertext IS NOT NULL
      AND hmac_secret_nonce IS NOT NULL
      AND hmac_secret_key_version IS NOT NULL
    )
  );

ALTER TABLE pitwall.webhook_delivery_failures
  DROP CONSTRAINT IF EXISTS webhook_delivery_failures_subscription_id_fkey;
ALTER TABLE pitwall.webhook_delivery_failures
  ADD CONSTRAINT webhook_delivery_failures_subscription_id_fkey
  FOREIGN KEY (subscription_id) REFERENCES pitwall.webhook_subscriptions(id)
  ON DELETE CASCADE;

ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_actor_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_actor_check CHECK (
    actor IN (
      'rest:admin', 'mcp:session-id', 'mcp:admin', 'rest:lease',
      'rest:webhook', 'mcp', 'system'
    )
  );

ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_action_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_action_check CHECK (
    action IN (
      'create', 'update', 'delete', 'enable', 'disable', 'hibernate',
      'patch', 'renew', 'rotate', 'deactivate', 'activate'
    )
  );

ALTER TABLE pitwall.config_audit
  DROP CONSTRAINT IF EXISTS config_audit_entity_type_check;
ALTER TABLE pitwall.config_audit
  ADD CONSTRAINT config_audit_entity_type_check CHECK (
    entity_type IN (
      'capability', 'provider', 'volume', 'template', 'drill', 'lease',
      'webhook_subscription'
    )
  );

COMMENT ON COLUMN pitwall.webhook_subscriptions.hmac_secret_ciphertext IS
  'AES-GCM ciphertext; plaintext is never stored.';
COMMENT ON COLUMN pitwall.webhook_subscriptions.hmac_secret_key_version IS
  'Version in PITWALL_WEBHOOK_ENCRYPTION_KEYS used to encrypt this secret.';
