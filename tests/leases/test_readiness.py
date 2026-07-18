from __future__ import annotations

from pitwall.core.models import LeaseReadiness


def test_runtime_null_never_counts_as_ready() -> None:
    readiness = LeaseReadiness(
        runtime_seen_at=None,
        port_mappings_seen_at="2026-05-28T12:00:19Z",
        probe_passed_at="2026-05-28T12:00:34Z",
        probe_method="ssh_localhost",
    )
    assert readiness.has_active_signals is False


def test_all_signals_present_means_ready() -> None:
    readiness = LeaseReadiness(
        runtime_seen_at="2026-05-28T12:00:18Z",
        port_mappings_seen_at="2026-05-28T12:00:19Z",
        probe_passed_at="2026-05-28T12:00:34Z",
        probe_method="ssh_localhost",
    )
    assert readiness.has_active_signals is True


def test_port_mappings_null_never_counts_as_ready() -> None:
    readiness = LeaseReadiness(
        runtime_seen_at="2026-05-28T12:00:18Z",
        port_mappings_seen_at=None,
        probe_passed_at="2026-05-28T12:00:34Z",
        probe_method="ssh_localhost",
    )
    assert readiness.has_active_signals is False


def test_probe_passed_null_never_counts_as_ready() -> None:
    readiness = LeaseReadiness(
        runtime_seen_at="2026-05-28T12:00:18Z",
        port_mappings_seen_at="2026-05-28T12:00:19Z",
        probe_passed_at=None,
        probe_method="ssh_localhost",
    )
    assert readiness.has_active_signals is False
