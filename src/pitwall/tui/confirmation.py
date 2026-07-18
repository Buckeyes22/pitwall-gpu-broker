"""Confirmation tiers for future mutating TUI actions."""

from __future__ import annotations

from enum import StrEnum


class ConfirmTier(StrEnum):
    """Operator confirmation strength required before running an action."""

    NONE = "none"
    CONFIRM = "confirm"
    TYPE_TO_CONFIRM = "type_to_confirm"
    DOUBLE_CONFIRM = "double_confirm"


_ACTION_TIERS: dict[str, ConfirmTier] = {
    "overview.refresh": ConfirmTier.NONE,
    "lease.renew": ConfirmTier.CONFIRM,
    "lease.terminate": ConfirmTier.TYPE_TO_CONFIRM,
    "provider.disable": ConfirmTier.DOUBLE_CONFIRM,
}


def confirm_tier_for_action(action: str) -> ConfirmTier:
    """Return the confirmation tier for a TUI action id."""

    return _ACTION_TIERS.get(action, ConfirmTier.CONFIRM)


__all__ = [
    "ConfirmTier",
    "confirm_tier_for_action",
]
