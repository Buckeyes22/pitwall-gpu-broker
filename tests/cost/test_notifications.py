"""Tests for shared cost alert notification transports."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any

from pitwall.cost.notifications import (
    LogNotifier,
    ResendNotifier,
    get_notifier,
)


def test_log_notifier_logs_alert_and_returns_ok(caplog: Any) -> None:
    notifier = LogNotifier()

    with caplog.at_level(logging.WARNING, logger="pitwall.alerts"):
        result = notifier.send(subject="Budget alert", body="Spend crossed threshold")

    assert result.ok is True
    assert result.email_id is None
    assert result.error is None
    assert "Budget alert" in caplog.text
    assert "Spend crossed threshold" in caplog.text


def test_default_notifier_uses_log_notifier_without_resend_key(monkeypatch: Any) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    assert isinstance(get_notifier(), LogNotifier)


def test_notifications_module_does_not_import_resend_at_module_import(monkeypatch: Any) -> None:
    monkeypatch.delitem(sys.modules, "resend", raising=False)

    # Exercise a *fresh* execution of the module body via a private, throwaway
    # copy loaded from source — NOT importlib.reload() on the canonical module.
    # reload() rebinds LogNotifier/ResendNotifier/get_notifier to brand-new class
    # objects in the shared sys.modules entry, which permanently breaks
    # isinstance() checks in sibling tests that captured the originals at import
    # time (e.g. test_default_notifier_uses_log_notifier_without_resend_key). A
    # private copy executes the same import-time code without mutating the shared
    # module, so the "no eager `import resend`" property is verified hermetically.
    import importlib.util

    canonical = importlib.import_module("pitwall.cost.notifications")
    spec = importlib.util.spec_from_file_location(
        "pitwall.cost._notifications_import_probe", canonical.__file__
    )
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    # Register the probe under its private name during exec so the module body's
    # @dataclass definitions can resolve their own __module__ (dataclasses looks
    # the class's module up in sys.modules). monkeypatch removes it on teardown;
    # the canonical "pitwall.cost.notifications" entry is never touched.
    monkeypatch.setitem(sys.modules, spec.name, probe)
    spec.loader.exec_module(probe)

    assert "resend" not in sys.modules


def test_resend_notifier_uses_canonical_alert_env(monkeypatch: Any) -> None:
    sent: list[dict[str, Any]] = []
    fake_resend = SimpleNamespace(
        api_key="",
        Emails=SimpleNamespace(send=lambda params: sent.append(params) or {"id": "email_123"}),
    )
    monkeypatch.setitem(sys.modules, "resend", fake_resend)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("PITWALL_ALERT_FROM", "alerts@example.com")
    monkeypatch.setenv("PITWALL_ALERT_TO", "admin@example.com")
    monkeypatch.delenv("RESEND_SENDER_EMAIL", raising=False)
    monkeypatch.delenv("RESEND_BUDGET_ALERT_EMAIL", raising=False)

    result = ResendNotifier().send(subject="Budget alert", body="Body text")

    assert result.ok is True
    assert result.email_id == "email_123"
    assert result.error is None
    assert fake_resend.api_key == "re_test_key"
    assert sent == [
        {
            "from": "alerts@example.com",
            "to": ["admin@example.com"],
            "subject": "Budget alert",
            "text": "Body text",
        }
    ]


def test_resend_notifier_uses_legacy_env_as_fallback(monkeypatch: Any) -> None:
    sent: list[dict[str, Any]] = []
    fake_resend = SimpleNamespace(
        api_key="",
        Emails=SimpleNamespace(send=lambda params: sent.append(params) or {"id": "email_legacy"}),
    )
    monkeypatch.setitem(sys.modules, "resend", fake_resend)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.delenv("PITWALL_ALERT_FROM", raising=False)
    monkeypatch.delenv("PITWALL_ALERT_TO", raising=False)
    monkeypatch.setenv("RESEND_SENDER_EMAIL", "legacy-from@example.com")
    monkeypatch.setenv("RESEND_BUDGET_ALERT_EMAIL", "legacy-to@example.com")

    result = ResendNotifier().send(subject="Legacy alert", body="Body text")

    assert result.ok is True
    assert result.email_id == "email_legacy"
    assert sent[0]["from"] == "legacy-from@example.com"
    assert sent[0]["to"] == ["legacy-to@example.com"]


def test_alert_config_prefers_canonical_env_with_legacy_fallback(monkeypatch: Any) -> None:
    from pitwall.config import load_settings_from_env

    monkeypatch.setenv("PITWALL_ALERT_FROM", "alerts@example.com")
    monkeypatch.setenv("PITWALL_ALERT_TO", "admin@example.com")
    monkeypatch.setenv("RESEND_SENDER_EMAIL", "legacy-from@example.com")
    monkeypatch.setenv("RESEND_BUDGET_ALERT_EMAIL", "legacy-to@example.com")

    settings = load_settings_from_env()

    assert settings.pitwall_alert_from == "alerts@example.com"
    assert settings.pitwall_alert_to == "admin@example.com"
    assert settings.resend_sender_email == "legacy-from@example.com"
    assert settings.resend_budget_alert_email == "legacy-to@example.com"

    monkeypatch.delenv("PITWALL_ALERT_FROM")
    monkeypatch.delenv("PITWALL_ALERT_TO")

    fallback_settings = load_settings_from_env()

    assert fallback_settings.pitwall_alert_from == "legacy-from@example.com"
    assert fallback_settings.pitwall_alert_to == "legacy-to@example.com"


def test_resend_notifier_returns_error_when_sdk_missing(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "resend", None)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("PITWALL_ALERT_FROM", "alerts@example.com")
    monkeypatch.setenv("PITWALL_ALERT_TO", "admin@example.com")

    result = ResendNotifier().send(subject="Budget alert", body="Body text")

    assert result.ok is False
    assert result.email_id is None
    assert result.error is not None
    assert "pitwall[email]" in result.error
