from __future__ import annotations

import pytest

from pitwall.live import is_live, require_live


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("RUNPOD_LIVE", "PITWALL_RUN_LIVE", "PITWALL_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


class TestIsLive:
    def test_false_with_no_env(self) -> None:
        assert is_live() is False

    def test_false_with_only_runpod_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNPOD_LIVE", "1")
        assert is_live() is False

    def test_false_with_only_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall:8080")
        assert is_live() is False

    def test_true_with_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNPOD_LIVE", "1")
        monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall:8080")
        assert is_live() is True

    def test_alias_pitwall_run_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PITWALL_RUN_LIVE", "1")
        monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall:8080")
        assert is_live() is True

    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("1", "true", "True", "TRUE", "yes", "on"):
            monkeypatch.setenv("RUNPOD_LIVE", val)
            monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall:8080")
            assert is_live() is True, f"expected True for RUNPOD_LIVE={val!r}"
            monkeypatch.setenv("RUNPOD_LIVE", "0")

    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall:8080")
        for val in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("RUNPOD_LIVE", val)
            assert is_live() is False, f"expected False for RUNPOD_LIVE={val!r}"

    def test_empty_base_url_is_not_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNPOD_LIVE", "1")
        monkeypatch.setenv("PITWALL_BASE_URL", "  ")
        assert is_live() is False


class TestRequireLive:
    def test_exits_without_live_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall:8080")
        with pytest.raises(SystemExit, match="1"):
            require_live()

    def test_exits_without_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNPOD_LIVE", "1")
        with pytest.raises(SystemExit, match="1"):
            require_live()

    def test_succeeds_with_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNPOD_LIVE", "1")
        monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall:8080")
        require_live()
