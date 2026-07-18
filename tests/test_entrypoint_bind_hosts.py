from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    (
        "module_name",
        "env_name",
        "port_name",
        "concurrency_name",
        "default_port",
        "default_concurrency",
        "app_path",
    ),
    [
        (
            "pitwall.api.__main__",
            "PITWALL_API_HOST",
            "PITWALL_API_PORT",
            "PITWALL_API_MAX_CONCURRENCY",
            8080,
            100,
            "pitwall.api.app:app",
        ),
        (
            "pitwall.webhook_receiver.__main__",
            "PITWALL_WEBHOOK_HOST",
            "PITWALL_WEBHOOK_RECEIVER_PORT",
            "PITWALL_WEBHOOK_MAX_CONCURRENCY",
            8082,
            50,
            "pitwall.webhook_receiver:app",
        ),
        (
            "pitwall.cost_exporter.__main__",
            "PITWALL_COST_EXPORTER_HOST",
            "PITWALL_COST_EXPORTER_PORT",
            "PITWALL_COST_EXPORTER_MAX_CONCURRENCY",
            9109,
            20,
            "pitwall.cost_exporter:app",
        ),
    ],
)
def test_bare_entrypoints_bind_loopback_by_default(
    module_name: str,
    env_name: str,
    port_name: str,
    concurrency_name: str,
    default_port: int,
    default_concurrency: int,
    app_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pytest.importorskip(module_name)
    calls: list[dict[str, object]] = []

    monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv(port_name, raising=False)
    monkeypatch.delenv(concurrency_name, raising=False)
    monkeypatch.setattr(
        module.uvicorn,
        "run",
        lambda app, **kwargs: calls.append({"app": app, **kwargs}),
    )

    module.main()

    assert calls == [
        {
            "app": app_path,
            "host": "127.0.0.1",
            "port": default_port,
            "limit_concurrency": default_concurrency,
        }
    ]


@pytest.mark.parametrize(
    ("module_name", "env_name", "port_name"),
    [
        ("pitwall.api.__main__", "PITWALL_API_HOST", "PITWALL_API_PORT"),
        (
            "pitwall.webhook_receiver.__main__",
            "PITWALL_WEBHOOK_HOST",
            "PITWALL_WEBHOOK_RECEIVER_PORT",
        ),
        (
            "pitwall.cost_exporter.__main__",
            "PITWALL_COST_EXPORTER_HOST",
            "PITWALL_COST_EXPORTER_PORT",
        ),
    ],
)
def test_bare_entrypoints_allow_configured_bind_host(
    module_name: str,
    env_name: str,
    port_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pytest.importorskip(module_name)
    calls: list[dict[str, object]] = []

    monkeypatch.setenv(env_name, "192.0.2.10")
    monkeypatch.setenv(port_name, "9191")
    monkeypatch.setattr(
        module.uvicorn,
        "run",
        lambda app, **kwargs: calls.append({"app": app, **kwargs}),
    )

    module.main()

    assert calls[0]["host"] == "192.0.2.10"
    assert calls[0]["port"] == 9191


@pytest.mark.parametrize(
    ("module_name", "concurrency_name"),
    [
        ("pitwall.api.__main__", "PITWALL_API_MAX_CONCURRENCY"),
        ("pitwall.webhook_receiver.__main__", "PITWALL_WEBHOOK_MAX_CONCURRENCY"),
        ("pitwall.cost_exporter.__main__", "PITWALL_COST_EXPORTER_MAX_CONCURRENCY"),
    ],
)
def test_bare_entrypoints_reject_nonpositive_concurrency(
    module_name: str,
    concurrency_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pytest.importorskip(module_name)
    monkeypatch.setenv(concurrency_name, "0")
    with pytest.raises(SystemExit, match="must be at least 1"):
        module.main()
