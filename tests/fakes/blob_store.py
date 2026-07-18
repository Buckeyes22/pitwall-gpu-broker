"""Async blob-store fake used by tests that need S3/R2 semantics."""

from __future__ import annotations

import datetime as dt
from typing import Any


class FakeBlobStore:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str, str]] = []

    async def list_objects(self, bucket: str, prefix: str = "") -> list[dict[str, Any]]:
        self.calls.append(("list_objects", bucket, prefix))
        return [
            {"key": key, "size": len(data), "last_modified": dt.datetime(2026, 5, 19)}
            for (b, key), data in self.store.items()
            if b == bucket and key.startswith(prefix)
        ]

    async def put_bytes(
        self, bucket: str, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        self.calls.append(("put_bytes", bucket, key))
        self.store[(bucket, key)] = data

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        self.calls.append(("get_bytes", bucket, key))
        return self.store[(bucket, key)]

    async def delete_object(self, bucket: str, key: str) -> None:
        self.calls.append(("delete_object", bucket, key))
        self.store.pop((bucket, key), None)


FakeGarage = FakeBlobStore
_FakeGarage = FakeBlobStore

__all__ = ["FakeBlobStore", "FakeGarage", "_FakeGarage"]
