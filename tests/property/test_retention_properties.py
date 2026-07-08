"""Property-based tests for retention's pure serialization seam.

SCOPE NOTE (verified against src/pitwall/retention/archive.py 2026-05-30):
The plan's Task 5 assumed a *pure window-math* function (cutoff/retain). There
is none — the retention window is inline SQL inside the async, DB-bound
``archive_workloads_to_jsonl`` (``WHERE submitted_at < NOW() - INTERVAL
'{older_than_days} days'``), so the window behavior can only be tested against a
real Postgres (release program / integration). The one genuinely pure unit here is
``_row_to_dict``, the row->JSON serialization seam every archived record passes
through. These properties pin its contract: output is always JSON-serializable,
keys are preserved, datetimes become ISO strings, and dict/list/None pass through.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.retention.archive import _row_to_dict

pytestmark = pytest.mark.property


class _FakeRecord:
    """Minimal asyncpg.Record stand-in: keys() + __getitem__ over a dict."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]


# JSON-native scalar values plus datetimes (which _row_to_dict isoformat()s).
_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=40),
)
_values = st.one_of(
    _json_scalars,
    st.lists(_json_scalars, max_size=5),
    st.dictionaries(st.text(min_size=1, max_size=8), _json_scalars, max_size=5),
    st.datetimes(min_value=dt.datetime(2000, 1, 1), max_value=dt.datetime(2100, 1, 1)).map(
        lambda d: d.replace(tzinfo=dt.UTC)
    ),
)
_rows = st.dictionaries(st.text(min_size=1, max_size=12), _values, max_size=8)


@given(row=_rows)
def test_output_is_json_serializable(row: dict) -> None:
    out = _row_to_dict(_FakeRecord(row))
    # Must never raise — the whole point is downstream json.dumps to JSONL.
    json.dumps(out)


@given(row=_rows)
def test_keys_are_preserved(row: dict) -> None:
    out = _row_to_dict(_FakeRecord(row))
    assert set(out.keys()) == set(row.keys())


@given(
    key=st.text(min_size=1, max_size=8),
    when=st.datetimes(min_value=dt.datetime(2000, 1, 1), max_value=dt.datetime(2100, 1, 1)).map(
        lambda d: d.replace(tzinfo=dt.UTC)
    ),
)
def test_datetime_becomes_isoformat_string(key: str, when: dt.datetime) -> None:
    out = _row_to_dict(_FakeRecord({key: when}))
    assert out[key] == when.isoformat()
    assert isinstance(out[key], str)


@given(
    key=st.text(min_size=1, max_size=8),
    value=st.one_of(
        st.dictionaries(st.text(min_size=1, max_size=6), st.integers(), max_size=4),
        st.lists(st.integers(), max_size=5),
    ),
)
def test_dict_and_list_pass_through_unchanged(key: str, value) -> None:
    out = _row_to_dict(_FakeRecord({key: value}))
    assert out[key] == value


def test_none_passes_through() -> None:
    out = _row_to_dict(_FakeRecord({"a": None, "b": 1}))
    assert out["a"] is None
    assert out["b"] == 1
