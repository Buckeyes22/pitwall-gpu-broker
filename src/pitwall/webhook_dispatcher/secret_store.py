"""Versioned AES-GCM encryption for webhook signing secrets at rest."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AAD = b"pitwall:webhook-signing-secret:v1"


@dataclass(frozen=True, slots=True)
class EncryptedWebhookSecret:
    ciphertext: bytes
    nonce: bytes
    key_version: str


class WebhookSecretCipher:
    """Encrypt and decrypt subscription secrets with an explicit current key."""

    def __init__(self, keys: Mapping[str, bytes], current_version: str) -> None:
        if current_version not in keys:
            raise ValueError("current webhook encryption key version is not configured")
        if not keys:
            raise ValueError("at least one webhook encryption key is required")
        for version, key in keys.items():
            if not version or len(key) != 32:
                raise ValueError("webhook encryption keys must be named 32-byte keys")
        self._keys = dict(keys)
        self.current_version = current_version

    @classmethod
    def from_env(cls) -> WebhookSecretCipher:
        raw = os.environ.get("PITWALL_WEBHOOK_ENCRYPTION_KEYS", "")
        current = os.environ.get("PITWALL_WEBHOOK_ENCRYPTION_CURRENT_KEY", "")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("PITWALL_WEBHOOK_ENCRYPTION_KEYS must be a JSON object") from exc
        if not isinstance(payload, Mapping) or not payload:
            raise ValueError("PITWALL_WEBHOOK_ENCRYPTION_KEYS must be a non-empty JSON object")
        keys: dict[str, bytes] = {}
        for version, encoded in payload.items():
            if not isinstance(version, str) or not isinstance(encoded, str):
                raise ValueError("webhook encryption key versions and values must be strings")
            try:
                keys[version] = base64.urlsafe_b64decode(encoded.encode())
            except (ValueError, TypeError) as exc:
                raise ValueError("webhook encryption keys must be URL-safe base64") from exc
        return cls(keys, current)

    def encrypt(self, secret: str) -> EncryptedWebhookSecret:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._keys[self.current_version]).encrypt(
            nonce,
            secret.encode(),
            _AAD,
        )
        return EncryptedWebhookSecret(ciphertext, nonce, self.current_version)

    def decrypt(self, encrypted: EncryptedWebhookSecret) -> str:
        key = self._keys.get(encrypted.key_version)
        if key is None:
            raise ValueError("webhook secret uses an unavailable encryption key version")
        plaintext = AESGCM(key).decrypt(encrypted.nonce, encrypted.ciphertext, _AAD)
        return plaintext.decode()


__all__ = ["EncryptedWebhookSecret", "WebhookSecretCipher"]
