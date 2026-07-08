"""Tests for the repo-wide text policy guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.guards import repo_text_policy


def test_guard_fails_on_runpod_shape_patterns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leak_file = tmp_path / "leak.txt"
    fake_endpoint_id = "abc123" + "def456gh"
    leak_file.write_text(
        "\n".join(
            [
                f"pod proxy https://{fake_endpoint_id}.proxy.runpod.net",
                f"pod proxy https://{fake_endpoint_id}-8000.proxy.runpod.net",
                f"queue api https://api.runpod.ai/v2/{fake_endpoint_id}/openai/v1",
            ]
        )
        + "\n"
    )

    result = repo_text_policy.main([str(leak_file)])

    captured = capsys.readouterr()
    assert result == 1
    assert "TP-SHAPE:" in captured.err
    assert "RunPod proxy hostname" in captured.err
    assert "RunPod API endpoint URL" in captured.err


def test_guard_fails_on_cgnat_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leak_file = tmp_path / "cgnat.txt"
    cgnat_ip = "100." + "127.255.254"
    leak_file.write_text(f"tailscale {cgnat_ip}\n")

    result = repo_text_policy.main([str(leak_file)])

    captured = capsys.readouterr()
    assert result == 1
    assert "TP-SHAPE:" in captured.err
    assert "Tailscale CGNAT address" in captured.err


def test_guard_allows_eptest_fixture_identifiers(tmp_path: Path) -> None:
    fixture_file = tmp_path / "fixture.txt"
    fixture_file.write_text(
        "\n".join(
            [
                "fake lb https://eptest00000000.api.runpod.ai/embed",
                "fake queue https://api.runpod.ai/v2/eptest00000001/openai/v1",
                "fake proxy https://eptest00000002.proxy.runpod.net",
                "fake proxy https://eptest00000003-8000.proxy.runpod.net",
            ]
        )
        + "\n"
    )

    assert repo_text_policy.main([str(fixture_file)]) == 0


def test_guard_allows_public_github_owner_url(tmp_path: Path) -> None:
    fixture_file = tmp_path / "github-url.txt"
    owner = "buck" + "eyes22"
    fixture_file.write_text(f"https://github.com/{owner}/pitwall\n")

    assert repo_text_policy.main([str(fixture_file)]) == 0


def test_guard_allows_public_ghcr_owner_image(tmp_path: Path) -> None:
    fixture_file = tmp_path / "ghcr-image.txt"
    owner = "buck" + "eyes22"
    fixture_file.write_text(f"ghcr.io/{owner}/pitwall/cloud-worker:latest\n")

    assert repo_text_policy.main([str(fixture_file)]) == 0


def test_guard_fails_on_bare_public_owner_handle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leak_file = tmp_path / "bare-owner.txt"
    owner = "buck" + "eyes22"
    leak_file.write_text(f"owner {owner}\n")

    result = repo_text_policy.main([str(leak_file)])

    captured = capsys.readouterr()
    assert result == 1
    assert "TP-OWNER:" in captured.err


def test_guard_allows_actions_fork_guard_idiom(tmp_path: Path) -> None:
    fixture_file = tmp_path / "workflow-guard.txt"
    owner = "buck" + "eyes22"
    fixture_file.write_text(f"    if: github.repository == '{owner}/pitwall' && ok\n")

    assert repo_text_policy.main([str(fixture_file)]) == 0


def test_guard_allows_codeowners_owner_mention(tmp_path: Path) -> None:
    fixture_file = tmp_path / "CODEOWNERS"
    owner = "buck" + "eyes22"
    fixture_file.write_text(f"* @{owner}\n")

    assert repo_text_policy.main([str(fixture_file)]) == 0


def test_guard_fails_on_deprecated_public_patterns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leak_file = tmp_path / "deprecated.txt"
    leak_file.write_text(
        "\n".join(
            [
                "fetch with " + "huggingface-cli" + " download model",
                "rotate via " + "/r2" + "/tokens",
                "field " + "r2_" + "credentials_" + "rotated",
            ]
        )
        + "\n"
    )

    result = repo_text_policy.main([str(leak_file)])

    captured = capsys.readouterr()
    assert result == 1
    assert "TP-DEPREC:" in captured.err
    assert "deprecated" in captured.err
    assert "zombie field" in captured.err


def test_guard_loads_extra_patterns_from_env_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay_file = tmp_path / "repo_text_policy.local"
    overlay_file.write_text(
        "\n".join(
            [
                "# local-only private patterns",
                "",
                r"private-overlay-token-[0-9]+",
            ]
        )
        + "\n"
    )
    leak_file = tmp_path / "local-leak.txt"
    leak_file.write_text("token private-overlay-token-42\n")
    monkeypatch.setenv("PITWALL_TEXT_POLICY_EXTRA", str(overlay_file))

    result = repo_text_policy.main([str(leak_file)])

    captured = capsys.readouterr()
    assert result == 1
    assert "LOCAL: private pattern" in captured.err


def test_guard_reports_invalid_extra_pattern(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay_file = tmp_path / "repo_text_policy.local"
    overlay_file.write_text("[unterminated\n")
    fixture_file = tmp_path / "fixture.txt"
    fixture_file.write_text("ordinary text\n")
    monkeypatch.setenv("PITWALL_TEXT_POLICY_EXTRA", str(overlay_file))

    result = repo_text_policy.main([str(fixture_file)])

    captured = capsys.readouterr()
    assert result == 2
    assert "invalid local text policy regex" in captured.err
