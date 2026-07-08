"""CLI dispatch tests for the Textual dashboard entry point."""

from __future__ import annotations

from pitwall import cli


def test_dashboard_command_runs_textual_app(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(self: object) -> None:
        calls.append(type(self).__name__)

    monkeypatch.setattr("pitwall.tui.app.PitwallApp.run", fake_run)

    assert cli.main(["dashboard"]) == 0
    assert calls == ["PitwallApp"]
