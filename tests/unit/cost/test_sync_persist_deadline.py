from __future__ import annotations

from pitwall.audit.sixteen_check import SYNC_RESULT_RETENTION_S


def test_sync_persist_deadline_is_inside_result_retention_window() -> None:
    assert SYNC_RESULT_RETENTION_S == 60
    assert SYNC_RESULT_RETENTION_S > 30
