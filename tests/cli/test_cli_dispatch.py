"""Characterization tests for the argparse-based ``pitwall`` CLI dispatcher.

All tests drive ``pitwall.cli.main`` / ``cmd_*`` with explicit argv and patch
``pitwall.cli`` names; nothing touches the network or a real DB. They lock in
current behavior (exit codes, dispatch routing, dry-run output).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall import cli


def test_main_no_args_prints_usage_and_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Usage: pitwall-gpu-broker" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("flag", ["-h", "--help", "help"])
def test_main_help_prints_stdout_and_returns_0(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main([flag])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Usage: pitwall-gpu-broker" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_main_version_prints_installed_version(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main([flag])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip()
    assert captured.err == ""


def test_main_unknown_group_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["bogus-group"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Unknown command group: bogus-group" in err
    assert "Usage: pitwall-gpu-broker" in err


def test_main_db_group_delegates_to_db_main() -> None:
    with patch("pitwall.db.main", return_value=0) as db_main:
        rc = cli.main(["db", "status"])
    assert rc == 0
    db_main.assert_called_once_with(["status"])


def test_main_register_template_routes_to_cmd() -> None:
    with patch("pitwall.cli.cmd_register_template", return_value=0) as cmd:
        rc = cli.main(["register-template", "--image", "ghcr.io/x/y:v1", "--dry-run"])
    assert rc == 0
    cmd.assert_called_once_with(["--image", "ghcr.io/x/y:v1", "--dry-run"])


def test_main_terminate_pod_routes_to_cmd() -> None:
    with patch("pitwall.cli.cmd_terminate_pod", return_value=0) as cmd:
        rc = cli.main(["terminate-pod", "--pod-id", "pod_123"])
    assert rc == 0
    cmd.assert_called_once_with(["--pod-id", "pod_123"])


def test_main_register_endpoint_routes_to_cmd() -> None:
    with patch("pitwall.cli.cmd_register_endpoint", return_value=0) as cmd:
        rc = cli.main(
            [
                "register-endpoint",
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                "c1",
                "--name",
                "n1",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
            ]
        )
    assert rc == 0
    cmd.assert_called_once_with(
        [
            "--endpoint-id",
            "e1",
            "--provider-type",
            "serverless_queue",
            "--capability-id",
            "c1",
            "--name",
            "n1",
            "--gpu-class",
            "NVIDIA H100 80GB HBM3",
        ]
    )


def test_main_set_provider_health_routes_to_cmd() -> None:
    with patch("pitwall.cli.cmd_set_provider_health", return_value=0) as cmd:
        rc = cli.main(["set-provider-health", "prov_123", "healthy"])
    assert rc == 0
    cmd.assert_called_once_with(["prov_123", "healthy"])


def test_main_warm_volume_routes_to_cmd() -> None:
    with patch("pitwall.cli.cmd_warm_volume", return_value=0) as cmd:
        rc = cli.main(["warm-volume", "--capability", "c1", "--volume-id", "v1"])
    assert rc == 0
    cmd.assert_called_once_with(["--capability", "c1", "--volume-id", "v1"])


def test_cmd_seed_routes_to_seed_async_not_warm_volume() -> None:
    with (
        patch("pitwall.cli._seed_async", new=AsyncMock(return_value=0)) as seed_async,
        patch("pitwall.cli._warm_volume_async", new=AsyncMock(return_value=0)) as warm_volume_async,
    ):
        rc = cli.cmd_seed(["seed/providers.yaml"])

    assert rc == 0
    seed_async.assert_awaited_once()
    args = seed_async.await_args.args[0]
    assert args.paths == ["seed/providers.yaml"]
    warm_volume_async.assert_not_called()


def test_main_mcp_routes_to_cmd() -> None:
    with patch("pitwall.cli.cmd_mcp_serve", return_value=0) as cmd:
        rc = cli.main(["mcp", "serve"])
    assert rc == 0
    cmd.assert_called_once_with(["serve"])


def test_main_config_check_routes_to_cmd() -> None:
    with patch("pitwall.cli.cmd_config", return_value=0) as cmd:
        rc = cli.main(["config", "check", "api"])
    assert rc == 0
    cmd.assert_called_once_with(["check", "api"])


def test_register_template_dry_run_is_hermetic(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.cmd_register_template(["--image", "ghcr.io/org/worker:v1", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] image: ghcr.io/org/worker:v1" in out
    assert "[dry-run] registry_auth_id:" in out
    assert "[dry-run] env_keys" in out


def test_warm_volume_dry_run_is_hermetic(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.cmd_warm_volume(
        ["--capability", "cap_llm_qwen3_32b", "--volume-id", "vol_1", "--dry-run"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] capability: cap_llm_qwen3_32b" in out
    assert "[dry-run] volume_id: vol_1" in out
    assert "[dry-run] provider: (auto-select)" in out


@pytest.mark.parametrize(
    ("pod", "expected"),
    [
        ({"desiredStatus": "EXITED"}, True),
        ({"desiredStatus": "TERMINATED"}, True),
        ({"desiredStatus": "RUNNING"}, False),
        ({}, False),
    ],
    ids=["exited", "terminated", "running", "empty"],
)
def test_is_terminated(pod: dict[str, object], expected: bool) -> None:
    assert cli._is_terminated(pod) is expected


def test_terminate_pod_missing_api_key_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    rc = cli.cmd_terminate_pod(["--pod-id", "pod_123"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "RUNPOD_API_KEY" in err


def test_terminate_pod_no_verify_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    with patch("pitwall.cli.terminate_pod_sync") as term:
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123", "--no-verify"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "terminate_pod called for pod_123" in out
    term.assert_called_once_with("pod_123")


def test_terminate_pod_raise_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    with patch("pitwall.cli.terminate_pod_sync", side_effect=RuntimeError("boom")):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_x"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "raised" in err
    assert "Manual teardown" in err


def test_terminate_pod_verify_pod_gone_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", return_value=None),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no longer returned by RunPod" in out


def test_terminate_pod_verify_reaches_exited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", return_value={"desiredStatus": "EXITED"}),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "desiredStatus=EXITED" in out


def test_parse_mcp_serve_args_defaults() -> None:
    ns = cli._parse_mcp_serve_args([])
    assert ns.transport == "stdio"


def test_parse_mcp_serve_args_rejects_network_transport() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._parse_mcp_serve_args(["--transport", "sse"])
    assert exc_info.value.code == 2


def test_parse_terminate_pod_args_defaults() -> None:
    ns = cli._parse_terminate_pod_args(["--pod-id", "pod_x"])
    assert ns.pod_id == "pod_x"
    assert ns.no_verify is False
    assert ns.verify_timeout_s == cli._TERMINATE_VERIFY_TIMEOUT_S


def test_parse_terminate_pod_args_no_verify() -> None:
    ns = cli._parse_terminate_pod_args(["--pod-id", "pod_x", "--no-verify"])
    assert ns.no_verify is True


def test_parse_terminate_pod_args_custom_timeout() -> None:
    ns = cli._parse_terminate_pod_args(["--pod-id", "pod_x", "--verify-timeout-s", "30.0"])
    assert ns.verify_timeout_s == 30.0


def test_parse_register_template_args_defaults() -> None:
    ns = cli._parse_register_template_args(["--image", "img:v1"])
    assert ns.image == "img:v1"
    assert ns.template_name == "pitwall-cloud-worker"
    assert ns.container_disk_gb == 50
    assert ns.dry_run is False


def test_parse_register_template_args_dry_run() -> None:
    ns = cli._parse_register_template_args(["--image", "img:v1", "--dry-run"])
    assert ns.dry_run is True


def test_parse_register_template_args_custom_template() -> None:
    ns = cli._parse_register_template_args(
        ["--image", "img:v1", "--template-name", "my-template", "--container-disk-gb", "100"]
    )
    assert ns.template_name == "my-template"
    assert ns.container_disk_gb == 100


def test_parse_warm_volume_args_defaults() -> None:
    ns = cli._parse_warm_volume_args(["--capability", "c1", "--volume-id", "v1"])
    assert ns.capability == "c1"
    assert ns.volume_id == "v1"
    assert ns.provider is None
    assert ns.script == "default"
    assert ns.dry_run is False
    assert ns.timeout == cli._WARM_VOLUME_DEFAULT_TIMEOUT_S


def test_parse_warm_volume_args_full() -> None:
    ns = cli._parse_warm_volume_args(
        [
            "--capability",
            "c1",
            "--volume-id",
            "v1",
            "--provider",
            "prov_x",
            "--script",
            "custom",
            "--dry-run",
            "--timeout",
            "600",
        ]
    )
    assert ns.provider == "prov_x"
    assert ns.script == "custom"
    assert ns.dry_run is True
    assert ns.timeout == 600


def test_parse_register_endpoint_args_required_only() -> None:
    ns = cli._parse_register_endpoint_args(
        [
            "--endpoint-id",
            "e1",
            "--provider-type",
            "serverless_queue",
            "--capability-id",
            "c1",
            "--name",
            "my-provider",
            "--gpu-class",
            "NVIDIA H100 80GB HBM3",
        ]
    )
    assert ns.endpoint_id == "e1"
    assert ns.provider_type == "serverless_queue"
    assert ns.capability_id == "c1"
    assert ns.name == "my-provider"
    assert ns.gpu_class == "NVIDIA H100 80GB HBM3"
    assert ns.region is None
    assert ns.cost_mode is None
    assert ns.workers_min == 0
    assert ns.idle_timeout_minutes == 0
    assert ns.flash_boot_verified is False
    assert ns.max_payload_mb == 30
    assert ns.request_timeout_s == 330
    assert ns.priority == 0
    assert ns.health == "unknown"


def test_parse_register_endpoint_args_all_options() -> None:
    ns = cli._parse_register_endpoint_args(
        [
            "--endpoint-id",
            "e1",
            "--provider-type",
            "serverless_lb",
            "--capability-id",
            "c1",
            "--name",
            "my-provider",
            "--gpu-class",
            "NVIDIA H100 80GB HBM3",
            "--capability-name",
            "llm.qwen3-32b",
            "--region",
            "US-KS-2",
            "--cost-mode",
            "per_second",
            "--per-second-active",
            "0.0001",
            "--per-request",
            "0.002",
            "--per-million-input-tokens",
            "0.5",
            "--per-million-output-tokens",
            "1.5",
            "--workers-min",
            "1",
            "--workers-max",
            "10",
            "--idle-timeout-minutes",
            "5",
            "--flash-boot-verified",
            "--max-payload-mb",
            "50",
            "--request-timeout-s",
            "60",
            "--priority",
            "3",
            "--health",
            "healthy",
        ]
    )
    assert ns.capability_name == "llm.qwen3-32b"
    assert ns.region == "US-KS-2"
    assert ns.cost_mode == "per_second"
    assert ns.per_second_active == 0.0001
    assert ns.per_request == 0.002
    assert ns.per_million_input_tokens == 0.5
    assert ns.per_million_output_tokens == 1.5
    assert ns.workers_min == 1
    assert ns.workers_max == 10
    assert ns.idle_timeout_minutes == 5
    assert ns.flash_boot_verified is True
    assert ns.max_payload_mb == 50
    assert ns.request_timeout_s == 60
    assert ns.priority == 3
    assert ns.health == "healthy"


def test_parse_set_provider_health_args() -> None:
    ns = cli._parse_set_provider_health_args(["prov_123", "healthy"])
    assert ns.provider_id == "prov_123"
    assert ns.health == "healthy"


def test_build_prewarm_script_default() -> None:
    script = cli._build_prewarm_script("default", "cap_llm_qwen3_32b")
    assert "PREWARM_START" in script
    assert "PREWARM_COMPLETE" in script
    assert "capability=" in script


def test_build_prewarm_script_custom() -> None:
    script = cli._build_prewarm_script("my-script", "cap_llm_qwen3_32b")
    assert "PREWARM_START" in script
    assert "PREWARM_COMPLETE" in script
    assert "script=my-script" in script


def test_register_template_async_exception_handler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def raise_error(args):
        raise RuntimeError("db connection failed")

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_register_template_async", raise_error)
    rc = cli.cmd_register_template(["--image", "ghcr.io/org/worker:v1"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Error:" in err


def test_register_endpoint_async_exception_handler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def raise_error(args):
        raise RuntimeError("endpoint error")

    monkeypatch.setattr(cli, "_register_endpoint_async", raise_error)
    rc = cli.cmd_register_endpoint(
        [
            "--endpoint-id",
            "e1",
            "--provider-type",
            "serverless_queue",
            "--capability-id",
            "c1",
            "--name",
            "n1",
            "--gpu-class",
            "NVIDIA H100 80GB HBM3",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "Error:" in err


def test_warm_volume_async_exception_handler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def raise_error(args):
        raise RuntimeError("warm error")

    monkeypatch.setattr(cli, "_warm_volume_async", raise_error)
    rc = cli.cmd_warm_volume(["--capability", "c1", "--volume-id", "v1"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Error:" in err


def test_terminate_pod_verify_get_pod_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    call_count = [0]

    def fake_get_pod(pod_id):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("pod API temporarily unavailable")
        return None

    monkeypatch.setattr(time, "sleep", lambda s: None)
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", side_effect=fake_get_pod),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123", "--verify-timeout-s", "0.1"])
    out = capsys.readouterr().out
    err = capsys.readouterr().err
    assert rc == 0
    assert "no longer returned by RunPod" in out or "WARN" in err or "OK" in out


def test_terminate_pod_verify_timeout_returns_manual_verification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", return_value={"desiredStatus": "RUNNING"}),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123", "--verify-timeout-s", "0.0"])
    out = capsys.readouterr().out
    err = capsys.readouterr().err
    assert rc == 1
    assert "did not reach EXITED/TERMINATED" in out or "Manual verification" in err


def test_register_template_non_dry_run_missing_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    rc = cli.cmd_register_template(["--image", "ghcr.io/org/worker:v1"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "RUNPOD_API_KEY" in err


def test_terminate_pod_verify_waiting_loop_reaches_terminated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    call_count = [0]

    def fake_get_pod(pod_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"desiredStatus": "RUNNING"}
        return {"desiredStatus": "TERMINATED"}

    monkeypatch.setattr(time, "sleep", lambda s: None)
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", side_effect=fake_get_pod),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123", "--verify-timeout-s", "15"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "desiredStatus=TERMINATED" in out


def test_terminate_pod_verify_waiting_message_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    call_count = [0]

    def fake_get_pod(pod_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"desiredStatus": "RUNNING"}
        return {"desiredStatus": "EXITED"}

    monkeypatch.setattr(time, "sleep", lambda s: None)
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", side_effect=fake_get_pod),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123", "--verify-timeout-s", "15"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "... waiting" in out


def test_cmd_mcp_serve_calls_mcp_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    mcp_run_calls: list[str] = []
    fake_mcp = type(
        "FakeMCP",
        (),
        {"run": lambda self, **kw: mcp_run_calls.append(kw.get("transport", "stdio"))},
    )()

    def noop(*args, **kwargs) -> None:
        pass

    class FakeMcpModule:
        mcp = fake_mcp
        ensure_runtime_env = noop

    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pitwall.mcp":
            return FakeMcpModule()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = cli.cmd_mcp_serve(["--transport", "stdio"])
    assert rc == 0
    assert mcp_run_calls == ["stdio"]


def test_cmd_mcp_serve_rejects_network_transport() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mcp_serve(["--transport", "sse"])
    assert exc_info.value.code == 2


def _make_mock_pool(fetchrow_side_effect=None) -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="OK")
    if fetchrow_side_effect:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    else:
        conn.fetchrow = AsyncMock(return_value={"id": "tpl_123"})

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


def _make_warm_volume_pool(cap_record=None, provider_record=None):
    def fetchrow_side_effect(sql, *args):
        if "capability" in sql.lower():
            if cap_record:
                return cap_record
            return {
                "id": "cap_123",
                "name": "llm.qwen3-32b",
                "class": "llm",
                "cost_mode": "per_second",
                "openai_compatible": True,
                "config": {},
                "version": "1.0",
                "description": None,
                "input_schema": {},
                "output_schema": {},
                "defaults": {},
                "hints_supported": [],
                "source": "api",
                "last_applied_yaml_hash": None,
                "enabled": True,
                "created_at": None,
                "updated_at": None,
            }
        if provider_record:
            return provider_record
        return {
            "id": "prov_456",
            "name": "test-prov",
            "priority": 1,
            "config": {"gpu_class": "NVIDIA H100 80GB HBM3"},
            "enabled": True,
            "health_status": "healthy",
        }

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="OK")
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


async def _mock_existing_capability(*args, **kwargs):
    return MagicMock(id=args[-1])


@pytest.mark.anyio
async def test_register_template_async_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_template_id = "tpl_123"

    async def mock_ensure_template(pool, *args, **kwargs):
        return mock_template_id

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=_make_mock_pool())),
        patch("pitwall.cli.ensure_template", new=mock_ensure_template),
    ):
        ns = cli._parse_register_template_args(["--image", "ghcr.io/org/worker:v1"])
        rc = await cli._register_template_async(ns)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Template registered: tpl_123" in out


@pytest.mark.anyio
async def test_register_endpoint_async_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def mock_upsert(*args, **kwargs):
        return MagicMock(id="cap_xyz")

    async def mock_create(*args, **kwargs):
        return MagicMock(
            id="prov_new",
            name="test-provider",
            capability_id="c1",
            provider_type=MagicMock(value="serverless_queue"),
            runpod_endpoint_id="e1",
            region=None,
            priority=0,
        )

    async def mock_get_by_name(*args, **kwargs):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    def make_endpoint_pool():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="OK")

        def fetchrow_side_effect(sql, *args):
            if "capability_name" in sql:
                return None
            return {
                "id": "prov_new",
                "name": "test-provider",
                "capability_id": "c1",
                "provider_type": "serverless_queue",
                "runpod_endpoint_id": "e1",
                "region": None,
                "priority": 0,
                "config": {},
                "enabled": True,
                "health_status": "unknown",
                "consecutive_failures": 0,
                "cooldown_trips": 0,
                "cold_start_p50_ms": None,
                "cold_start_p95_ms": None,
                "recent_error_rate": 0.0,
                "cooldown_until": None,
                "source": "api",
                "last_applied_yaml_hash": None,
                "updated_at": None,
            }

        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=make_endpoint_pool())),
        patch("pitwall.db.repository.CapabilityRepository.get", new=_mock_existing_capability),
        patch("pitwall.db.repository.CapabilityRepository.upsert", new=mock_upsert),
        patch("pitwall.db.repository.ProviderRepository.get_by_name", new=mock_get_by_name),
        patch("pitwall.db.repository.ProviderRepository.create", new=mock_create),
    ):
        ns = cli._parse_register_endpoint_args(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                "c1",
                "--name",
                "test-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
            ]
        )
        rc = await cli._register_endpoint_async(ns)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Provider registered: prov_new" in out


def test_warm_volume_missing_datacenter_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.delenv("RUNPOD_DATA_CENTER_ID", raising=False)
    rc = cli.cmd_warm_volume(["--capability", "cap_llm_qwen3_32b", "--volume-id", "vol_abc"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "RUNPOD_DATA_CENTER_ID" in err


def test_warm_volume_missing_worker_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "US-KS-2")
    monkeypatch.delenv("PITWALL_CLOUD_WORKER_IMAGE", raising=False)

    rc = cli.cmd_warm_volume(["--capability", "cap_llm_qwen3_32b", "--volume-id", "vol_abc"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "PITWALL_CLOUD_WORKER_IMAGE is required" in err


def test_warm_volume_dry_run_includes_script_and_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.cmd_warm_volume(
        [
            "--capability",
            "cap_llm_qwen3_32b",
            "--volume-id",
            "vol_1",
            "--script",
            "my-script",
            "--timeout",
            "600",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] script: my-script" in out
    assert "[dry-run] timeout: 600s" in out


def test_register_template_dry_run_includes_sha_and_display_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.cmd_register_template(
        ["--image", "ghcr.io/org/worker:v1", "--template-name", "my-template", "--dry-run"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] image_sha:" in out
    assert "[dry-run] template_display_name:" in out
    assert "[dry-run] container_disk_gb:" in out


def test_warm_volume_missing_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "US-KS-2")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    rc = cli.cmd_warm_volume(["--capability", "cap_llm_qwen3_32b", "--volume-id", "vol_abc"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "RUNPOD_API_KEY" in err


def test_terminate_pod_verify_get_pod_exception_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    call_count = [0]

    def fake_get_pod(pod_id):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("pod API temporarily unavailable")
        return None

    monkeypatch.setattr(time, "sleep", lambda s: None)
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", side_effect=fake_get_pod),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123", "--verify-timeout-s", "0.1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no longer returned by RunPod" in out


def test_terminate_pod_verify_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    with (
        patch("pitwall.cli.terminate_pod_sync"),
        patch("pitwall.cli.get_pod_sync", return_value={"desiredStatus": "RUNNING"}),
    ):
        rc = cli.cmd_terminate_pod(["--pod-id", "pod_123", "--verify-timeout-s", "0.0"])
    out = capsys.readouterr().out
    err = capsys.readouterr().err
    assert rc == 1
    assert "did not reach EXITED/TERMINATED" in out or "Manual verification" in err


def test_parse_register_template_args_custom_container_disk(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ns = cli._parse_register_template_args(["--image", "img:v1", "--container-disk-gb", "100"])
    assert ns.container_disk_gb == 100


def test_parse_register_template_args_custom_name(capsys: pytest.CaptureFixture[str]) -> None:
    ns = cli._parse_register_template_args(
        ["--image", "img:v1", "--template-name", "my-custom-template"]
    )
    assert ns.template_name == "my-custom-template"


def test_parse_warm_volume_args_with_provider(capsys: pytest.CaptureFixture[str]) -> None:
    ns = cli._parse_warm_volume_args(
        ["--capability", "cap1", "--volume-id", "vol1", "--provider", "prov_x"]
    )
    assert ns.provider == "prov_x"


def test_register_endpoint_async_with_capability_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def mock_upsert(*args, **kwargs):
        return MagicMock(id="cap_xyz")

    async def mock_create(*args, **kwargs):
        return MagicMock(
            id="prov_new",
            name="my-provider",
            capability_id="c1",
            provider_type=MagicMock(value="serverless_queue"),
            runpod_endpoint_id="e1",
            region=None,
            priority=0,
        )

    async def mock_get_by_name(*args, **kwargs):
        return None

    def make_pool():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="OK")
        conn.fetchrow = AsyncMock(
            side_effect=[
                None,
                {
                    "id": "prov_new",
                    "name": "my-provider",
                    "capability_id": "c1",
                    "provider_type": "serverless_queue",
                    "runpod_endpoint_id": "e1",
                    "region": None,
                    "priority": 0,
                    "config": {},
                    "enabled": True,
                    "health_status": "unknown",
                    "consecutive_failures": 0,
                    "cooldown_trips": 0,
                    "cold_start_p50_ms": None,
                    "cold_start_p95_ms": None,
                    "recent_error_rate": 0.0,
                    "cooldown_until": None,
                    "source": "api",
                    "last_applied_yaml_hash": None,
                    "updated_at": None,
                },
            ]
        )
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=make_pool())),
        patch("pitwall.db.repository.CapabilityRepository.upsert", new=mock_upsert),
        patch("pitwall.db.repository.ProviderRepository.get_by_name", new=mock_get_by_name),
        patch("pitwall.db.repository.ProviderRepository.create", new=mock_create),
    ):
        cli._parse_register_endpoint_args(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                "c1",
                "--name",
                "my-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
                "--capability-name",
                "llm.qwen3-32b",
            ]
        )
        rc = cli.cmd_register_endpoint(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                "c1",
                "--name",
                "my-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
                "--capability-name",
                "llm.qwen3-32b",
            ]
        )

    assert rc == 0


def test_register_endpoint_async_provider_already_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def mock_get_by_name(*args, **kwargs):
        return MagicMock(id="prov_existing", name="my-provider")

    def make_pool():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="OK")
        conn.fetchrow = AsyncMock(
            return_value={
                "id": "prov_existing",
                "name": "my-provider",
                "capability_id": "c1",
                "provider_type": "serverless_queue",
                "runpod_endpoint_id": "e1",
                "region": None,
                "priority": 0,
                "config": {},
                "enabled": True,
                "health_status": "unknown",
                "consecutive_failures": 0,
                "cooldown_trips": 0,
                "cold_start_p50_ms": None,
                "cold_start_p95_ms": None,
                "recent_error_rate": 0.0,
                "cooldown_until": None,
                "source": "api",
                "last_applied_yaml_hash": None,
                "updated_at": None,
            }
        )
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=make_pool())),
        patch("pitwall.db.repository.ProviderRepository.get_by_name", new=mock_get_by_name),
    ):
        rc = cli.cmd_register_endpoint(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                "c1",
                "--name",
                "my-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
            ]
        )

    err = capsys.readouterr().err
    assert rc == 1
    assert "already exists" in err


@pytest.mark.anyio
async def test_register_endpoint_missing_capability_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def mock_capability_get(*args, **kwargs):
        return None

    async def mock_provider_get_by_name(*args, **kwargs):
        return None

    mock_create = AsyncMock(
        return_value=MagicMock(
            id="prov_new",
            name="my-provider",
            capability_id="cap_missing",
            provider_type=MagicMock(value="serverless_queue"),
            runpod_endpoint_id="e1",
            region=None,
            priority=0,
        )
    )

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=MagicMock())),
        patch("pitwall.db.repository.CapabilityRepository.get", new=mock_capability_get),
        patch(
            "pitwall.db.repository.ProviderRepository.get_by_name", new=mock_provider_get_by_name
        ),
        patch("pitwall.db.repository.ProviderRepository.create", new=mock_create),
    ):
        ns = cli._parse_register_endpoint_args(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                "cap_missing",
                "--name",
                "my-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
            ]
        )
        rc = await cli._register_endpoint_async(ns)

    err = capsys.readouterr().err
    assert rc == 1
    assert "capability 'cap_missing' does not exist" in err
    assert "create it first" in err
    mock_create.assert_not_awaited()


@pytest.mark.anyio
async def test_register_endpoint_then_mark_healthy_makes_provider_routable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import datetime as dt

    from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode
    from pitwall.core.models import Capability, Provider
    from pitwall.resolver.exceptions import NoHealthyProviderError
    from pitwall.resolver.service import select_stage12_provider
    from pitwall.routing import RoutingRequest

    now = dt.datetime(2026, 5, 31, 12, 0, 0, tzinfo=dt.UTC)
    capability = Capability(
        id="cap_routable",
        name="embedding.routable",
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=now,
        updated_at=now,
    )
    created_provider: Provider | None = None
    patched_provider: Provider | None = None

    async def mock_capability_get(*args, **kwargs):
        capability_id = args[-1]
        return capability if capability_id == capability.id else None

    async def mock_provider_get_by_name(*args, **kwargs):
        return None

    async def mock_provider_create(*args, **kwargs):
        nonlocal created_provider
        created_provider = args[-1]
        return created_provider

    async def mock_provider_get(*args, **kwargs):
        provider_id = args[-1]
        if created_provider is not None and provider_id == created_provider.id:
            return created_provider
        return None

    async def mock_provider_patch(*args, **kwargs):
        nonlocal patched_provider
        assert created_provider is not None
        patched_provider = created_provider.model_copy(
            update={
                "health_status": kwargs["health_status"],
                "consecutive_failures": kwargs["consecutive_failures"],
                "cooldown_trips": kwargs["cooldown_trips"],
                "recent_error_rate": kwargs["recent_error_rate"],
                "cooldown_until": kwargs["cooldown_until"],
            }
        )
        return patched_provider

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=MagicMock())),
        patch("pitwall.db.repository.CapabilityRepository.get", new=mock_capability_get),
        patch(
            "pitwall.db.repository.ProviderRepository.get_by_name", new=mock_provider_get_by_name
        ),
        patch("pitwall.db.repository.ProviderRepository.create", new=mock_provider_create),
    ):
        ns = cli._parse_register_endpoint_args(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                capability.id,
                "--name",
                "my-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
            ]
        )
        rc = await cli._register_endpoint_async(ns)

    assert rc == 0
    assert created_provider is not None
    assert created_provider.health_status == "unknown"
    request = RoutingRequest(capability_name=capability.name, capability_id=capability.id)
    with pytest.raises(NoHealthyProviderError):
        select_stage12_provider(request, [created_provider], capability=capability, now=now)

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=MagicMock())),
        patch("pitwall.db.repository.ProviderRepository.get", new=mock_provider_get),
        patch("pitwall.db.repository.ProviderRepository.patch", new=mock_provider_patch),
    ):
        health_args = cli._parse_set_provider_health_args([created_provider.id, "healthy"])
        health_rc = await cli._set_provider_health_async(health_args)

    out = capsys.readouterr().out
    assert health_rc == 0
    assert "health_status: healthy" in out
    assert patched_provider is not None
    resolution = select_stage12_provider(
        request,
        [patched_provider],
        capability=capability,
        now=now,
    )
    assert resolution.provider.id == created_provider.id


def test_register_endpoint_async_with_cost_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def mock_create(*args, **kwargs):
        return MagicMock(
            id="prov_new",
            name="my-provider",
            capability_id="c1",
            provider_type=MagicMock(value="serverless_queue"),
            runpod_endpoint_id="e1",
            region=None,
            priority=0,
        )

    async def mock_get_by_name(*args, **kwargs):
        return None

    def make_pool():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="OK")
        conn.fetchrow = AsyncMock(
            side_effect=[
                None,
                {
                    "id": "prov_new",
                    "name": "my-provider",
                    "capability_id": "c1",
                    "provider_type": "serverless_queue",
                    "runpod_endpoint_id": "e1",
                    "region": None,
                    "priority": 0,
                    "config": {},
                    "enabled": True,
                    "health_status": "unknown",
                    "consecutive_failures": 0,
                    "cooldown_trips": 0,
                    "cold_start_p50_ms": None,
                    "cold_start_p95_ms": None,
                    "recent_error_rate": 0.0,
                    "cooldown_until": None,
                    "source": "api",
                    "last_applied_yaml_hash": None,
                    "updated_at": None,
                },
            ]
        )
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=make_pool())),
        patch("pitwall.db.repository.CapabilityRepository.get", new=_mock_existing_capability),
        patch("pitwall.db.repository.ProviderRepository.get_by_name", new=mock_get_by_name),
        patch("pitwall.db.repository.ProviderRepository.create", new=mock_create),
    ):
        rc = cli.cmd_register_endpoint(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_queue",
                "--capability-id",
                "c1",
                "--name",
                "my-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
                "--cost-mode",
                "per_second",
                "--per-second-active",
                "0.0001",
                "--per-request",
                "0.002",
                "--per-million-input-tokens",
                "0.5",
                "--per-million-output-tokens",
                "1.5",
                "--workers-min",
                "1",
                "--workers-max",
                "10",
            ]
        )

    assert rc == 0


def test_register_endpoint_async_serverless_lb(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pitwall.core.enums import ProviderType

    class FakeProviderResult:
        def __init__(self, provider_type_val):
            self.provider_type = provider_type_val
            self.id = "prov_new"
            self.name = "my-lb-provider"
            self.capability_id = "c1"
            self.runpod_endpoint_id = "e1"
            self.region = "US-KS-2"
            self.priority = 0
            self.health_status = "unknown"

    async def mock_create(*args, **kwargs):
        return FakeProviderResult(provider_type_val=ProviderType.SERVERLESS_LB)

    async def mock_get_by_name(*args, **kwargs):
        return None

    def make_pool():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="OK")
        conn.fetchrow = AsyncMock(
            side_effect=[
                None,
                {
                    "id": "prov_new",
                    "name": "my-lb-provider",
                    "capability_id": "c1",
                    "provider_type": "serverless_lb",
                    "runpod_endpoint_id": "e1",
                    "region": "US-KS-2",
                    "priority": 0,
                    "config": {},
                    "enabled": True,
                    "health_status": "unknown",
                    "consecutive_failures": 0,
                    "cooldown_trips": 0,
                    "cold_start_p50_ms": None,
                    "cold_start_p95_ms": None,
                    "recent_error_rate": 0.0,
                    "cooldown_until": None,
                    "source": "api",
                    "last_applied_yaml_hash": None,
                    "updated_at": None,
                },
            ]
        )
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=make_pool())),
        patch("pitwall.db.repository.CapabilityRepository.get", new=_mock_existing_capability),
        patch("pitwall.db.repository.ProviderRepository.get_by_name", new=mock_get_by_name),
        patch("pitwall.db.repository.ProviderRepository.create", new=mock_create),
    ):
        rc = cli.cmd_register_endpoint(
            [
                "--endpoint-id",
                "e1",
                "--provider-type",
                "serverless_lb",
                "--capability-id",
                "c1",
                "--name",
                "my-lb-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
                "--region",
                "US-KS-2",
            ]
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Provider registered: prov_new" in out


def test_register_endpoint_async_public_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pitwall.core.enums import ProviderType

    class FakeProviderResult:
        def __init__(self, provider_type_val):
            self.provider_type = provider_type_val
            self.id = "prov_pub"
            self.name = "my-pub-provider"
            self.capability_id = "c1"
            self.runpod_endpoint_id = "pub1"
            self.region = "US-KS-2"
            self.priority = 0
            self.health_status = "unknown"

    async def mock_create(*args, **kwargs):
        return FakeProviderResult(provider_type_val=ProviderType.PUBLIC_ENDPOINT)

    async def mock_get_by_name(*args, **kwargs):
        return None

    def make_pool():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="OK")
        conn.fetchrow = AsyncMock(
            side_effect=[
                None,
                {
                    "id": "prov_pub",
                    "name": "my-pub-provider",
                    "capability_id": "c1",
                    "provider_type": "public_endpoint",
                    "runpod_endpoint_id": "pub1",
                    "region": "US-KS-2",
                    "priority": 0,
                    "config": {},
                    "enabled": True,
                    "health_status": "unknown",
                    "consecutive_failures": 0,
                    "cooldown_trips": 0,
                    "cold_start_p50_ms": None,
                    "cold_start_p95_ms": None,
                    "recent_error_rate": 0.0,
                    "cooldown_until": None,
                    "source": "api",
                    "last_applied_yaml_hash": None,
                    "updated_at": None,
                },
            ]
        )
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=make_pool())),
        patch("pitwall.db.repository.CapabilityRepository.get", new=_mock_existing_capability),
        patch("pitwall.db.repository.ProviderRepository.get_by_name", new=mock_get_by_name),
        patch("pitwall.db.repository.ProviderRepository.create", new=mock_create),
    ):
        rc = cli.cmd_register_endpoint(
            [
                "--endpoint-id",
                "pub1",
                "--provider-type",
                "public_endpoint",
                "--capability-id",
                "c1",
                "--name",
                "my-pub-provider",
                "--gpu-class",
                "NVIDIA H100 80GB HBM3",
                "--region",
                "US-KS-2",
            ]
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Provider registered: prov_pub" in out


class TestJsonFlag:
    """Tests for ``--json`` output on CLI commands."""

    def test_register_template_dry_run_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        rc = cli.cmd_register_template(["--image", "ghcr.io/org/worker:v1", "--dry-run", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data["image"] == "ghcr.io/org/worker:v1"
        assert "template_id" not in data

    def test_warm_volume_dry_run_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        rc = cli.cmd_warm_volume(
            ["--capability", "cap1", "--volume-id", "vol1", "--dry-run", "--json"]
        )
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data["capability"] == "cap1"
        assert data["volume_id"] == "vol1"

    def test_terminate_pod_missing_key_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        rc = cli.cmd_terminate_pod(["--pod-id", "pod_x", "--json"])
        out = capsys.readouterr().out
        assert rc == 1
        data = json.loads(out)
        assert "error" in data

    def test_config_check_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        rc = cli.cmd_config(["check", "api", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data["service"] == "api"
        assert data["status"] == "ok"
