"""ULID identifiers and helpers for Pitwall.

ULID identifiers are used throughout Pitwall. Each prefixed ULID takes the form
``{prefix}_{ulid}`` where
the ULID portion is a Crockford-base32 encoded 128-bit timestamp+random value.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"
"""Crockford-base32 ULID, 26 chars. Pattern excludes I, L, O, U to avoid 0/O and 1/I/L confusion."""


ULID = Annotated[
    str,
    Field(
        min_length=26,
        max_length=26,
        pattern=ULID_PATTERN,
        description="Crockford-base32 ULID, 26 characters",
    ),
]
"""Pydantic annotated type for validating ULID strings."""


def ulid_new() -> str:
    """Generate a new ULID string.

    Lazily imports python-ulid so importing this module stays fast when only
    types (not generators) are needed.
    """
    from ulid import ULID as _ULID

    return str(_ULID())


def is_valid_ulid(value: str) -> bool:
    """Return True if value is a valid 26-char Crockford-base32 ULID."""
    if len(value) != 26:
        return False
    return all(ch in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for ch in value)
