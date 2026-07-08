"""Real-Postgres proof that webhook secrets are encrypted and lifecycle-audited."""

from __future__ import annotations

import pytest

from pitwall.db.repository import WebhookSubscriptionRepository
from pitwall.webhook_dispatcher.secret_store import WebhookSecretCipher
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]


async def test_webhook_secret_is_never_plaintext_and_lifecycle_is_audited(pg_pool) -> None:
    cipher = WebhookSecretCipher({"v1": bytes(range(32))}, "v1")
    repo = WebhookSubscriptionRepository(pg_pool, cipher)
    original_secret = "original-consumer-signing-secret"
    rotated_secret = "rotated-consumer-signing-secret"

    created = await repo.create(
        "consumer-a",
        "https://hooks.example.test/events",
        hmac_secret=original_secret,
        actor="rest:webhook",
    )
    listed = await repo.list(consumer="consumer-a")
    dispatchable = await repo.list_for_dispatch(consumer="consumer-a")

    assert created.hmac_secret == original_secret
    assert listed[0].hmac_secret is None
    assert dispatchable[0].hmac_secret == original_secret
    async with pg_pool.acquire() as conn:
        stored_text = await conn.fetchval(
            "SELECT to_jsonb(webhook_subscriptions)::text "
            "FROM pitwall.webhook_subscriptions WHERE id = $1",
            int(created.id),
        )
        plaintext_column_count = await conn.fetchval(
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'pitwall'
              AND table_name = 'webhook_subscriptions'
              AND column_name = 'hmac_secret'
            """
        )
    assert original_secret not in stored_text
    assert plaintext_column_count == 0

    rotated = await repo.rotate_secret(
        int(created.id),
        rotated_secret,
        actor="rest:webhook",
    )
    assert rotated is not None
    assert rotated.hmac_secret == rotated_secret
    after_rotation = await repo.list_for_dispatch(consumer="consumer-a")
    assert after_rotation[0].hmac_secret == rotated_secret

    deactivated = await repo.deactivate(int(created.id), actor="rest:webhook")
    assert deactivated is not None
    assert deactivated.active is False
    assert await repo.list_for_dispatch(consumer="consumer-a") == []
    activated = await repo.activate(int(created.id), actor="rest:webhook")
    assert activated is not None
    assert activated.active is True
    assert await repo.delete(int(created.id), actor="rest:webhook") is True

    async with pg_pool.acquire() as conn:
        audit_actions = await conn.fetch(
            """
            SELECT action FROM pitwall.config_audit
            WHERE entity_type = 'webhook_subscription' AND entity_id = $1
            ORDER BY id
            """,
            created.id,
        )
    assert [row["action"] for row in audit_actions] == [
        "create",
        "rotate",
        "deactivate",
        "activate",
        "delete",
    ]
