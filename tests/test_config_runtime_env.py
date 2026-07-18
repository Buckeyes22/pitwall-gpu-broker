from __future__ import annotations

import os
from pathlib import Path

import pytest

from pitwall.config import (
    PitwallSettings,
    check_domain_config,
    is_loopback_host,
    load_settings_from_env,
    require_credentials_for_bind,
    require_runtime_env,
    required_runtime_env_vars,
)

_ALL_RUNTIME_ENV = (
    "RUNPOD_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "PITWALL_ADMIN_SECRET",
    "PITWALL_CONFIG_FILE",
    "PITWALL_MONTHLY_BUDGET_USD",
)


@pytest.fixture(autouse=True)
def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_RUNTIME_ENV:
        monkeypatch.delenv(name, raising=False)


def test_api_missing_runtime_env_exits_ex_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        require_runtime_env("api")

    assert raised.value.code == os.EX_CONFIG
    err = capsys.readouterr().err
    assert "RUNPOD_API_KEY" in err
    assert "DATABASE_URL" in err
    assert "REDIS_URL" in err


def test_api_accepts_required_env_without_admin_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://pitwall:pitwall@localhost/pitwall")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/4")

    require_runtime_env("pitwall-api")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost"])
def test_loopback_hosts_are_recognized(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "api.internal"])
def test_non_loopback_hosts_are_rejected_without_credentials(
    host: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        require_credentials_for_bind("api", host, ("PITWALL_API_TOKEN",))
    assert raised.value.code == os.EX_CONFIG
    assert "PITWALL_API_TOKEN" in capsys.readouterr().err


def test_non_loopback_bind_accepts_required_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PITWALL_API_TOKEN", "configured")
    require_credentials_for_bind("api", "0.0.0.0", ("PITWALL_API_TOKEN",))


def test_insecure_bind_override_is_explicit_and_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PITWALL_UNSAFE_ALLOW_INSECURE_BIND", "1")
    require_credentials_for_bind("api", "0.0.0.0", ("PITWALL_API_TOKEN",))
    assert "WARNING" in capsys.readouterr().err


def test_empty_runtime_env_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "   ")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/4")

    with pytest.raises(SystemExit) as raised:
        require_runtime_env("api")

    assert raised.value.code == os.EX_CONFIG


def test_cost_exporter_only_requires_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://pitwall:pitwall@localhost/pitwall")

    require_runtime_env("pitwall-cost-exporter")


def test_required_runtime_env_vars_defaults_to_core_for_unknown_service() -> None:
    assert required_runtime_env_vars("future-service") == (
        "RUNPOD_API_KEY",
        "DATABASE_URL",
        "REDIS_URL",
    )


def test_mcp_requires_core_runtime_env(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        require_runtime_env("mcp")

    assert raised.value.code == os.EX_CONFIG
    err = capsys.readouterr().err
    assert "RUNPOD_API_KEY" in err
    assert "DATABASE_URL" in err
    assert "REDIS_URL" in err


def test_mcp_accepts_required_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://pitwall:pitwall@localhost/pitwall")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/4")

    require_runtime_env("mcp")


def test_settings_load_default_pitwall_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pitwall.toml").write_text(
        """
runpod_api_key = "file-runpod-key"
database_url = "postgresql://file-db/pitwall"
redis_url = "redis://file-redis:6379/4"
pitwall_monthly_budget_usd = 125.5
""".lstrip(),
        encoding="utf-8",
    )

    settings = load_settings_from_env()

    assert settings.runpod_api_key == "file-runpod-key"
    assert settings.database_url == "postgresql://file-db/pitwall"
    assert settings.redis_url == "redis://file-redis:6379/4"
    assert settings.pitwall_monthly_budget_usd == 125.5


def test_settings_config_file_is_overridden_by_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "custom-pitwall.toml"
    config_file.write_text(
        """
RUNPOD_API_KEY = "file-runpod-key"
DATABASE_URL = "postgresql://file-db/pitwall"
REDIS_URL = "redis://file-redis:6379/4"
PITWALL_MONTHLY_BUDGET_USD = 125.5
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PITWALL_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("RUNPOD_API_KEY", "env-runpod-key")
    monkeypatch.setenv("PITWALL_MONTHLY_BUDGET_USD", "25.0")

    settings = load_settings_from_env()

    assert settings.runpod_api_key == "env-runpod-key"
    assert settings.database_url == "postgresql://file-db/pitwall"
    assert settings.redis_url == "redis://file-redis:6379/4"
    assert settings.pitwall_monthly_budget_usd == 25.0


def test_domain_config_check_flags_broken_config() -> None:
    settings = PitwallSettings(
        runpod_api_key="",
        database_url="",
        redis_url="redis://localhost:6379/4",
        pitwall_monthly_budget_usd=-1,
        pitwall_per_request_max_usd=-0.01,
        pitwall_embedding_via_pitwall=True,
        pitwall_base_url="",
    )

    result = check_domain_config("api", settings=settings)

    assert not result.ok
    error_text = "\n".join(issue.message for issue in result.errors)
    assert "RUNPOD_API_KEY" in error_text
    assert "DATABASE_URL" in error_text
    assert "PITWALL_MONTHLY_BUDGET_USD" in error_text
    assert "PITWALL_PER_REQUEST_MAX_USD" in error_text
    assert "PITWALL_BASE_URL" in error_text


def test_domain_config_check_passes_good_config() -> None:
    settings = PitwallSettings(
        runpod_api_key="test-key",
        database_url="postgresql://pitwall:pitwall@localhost/pitwall",
        redis_url="redis://localhost:6379/4",
        pitwall_monthly_budget_usd=50.0,
        pitwall_per_request_max_usd=10.0,
        r2_temp_credentials_enabled="false",
    )

    result = check_domain_config("api", settings=settings)

    assert result.ok
    assert result.errors == ()
