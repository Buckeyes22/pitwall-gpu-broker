"""Property tests for semantic cache key construction."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.routing.semantic_cache import build_semantic_cache_key

pytestmark = pytest.mark.property

_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters=("\x00",),
    ),
    min_size=1,
    max_size=24,
)
_JSON_SCALAR = st.none() | st.booleans() | st.integers() | _TEXT
_JSON_VALUE = st.recursive(
    _JSON_SCALAR,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(_TEXT, children, min_size=1, max_size=4)
    ),
    max_leaves=12,
)
_PARAMS = st.dictionaries(_TEXT, _JSON_VALUE, min_size=1, max_size=5)


@given(params=_PARAMS)
def test_semantic_cache_key_is_stable_for_dict_order(
    params: dict[str, object],
) -> None:
    reordered = dict(reversed(tuple(params.items())))

    assert build_semantic_cache_key(
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        capability_params=params,
    ) == build_semantic_cache_key(
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        capability_params=reordered,
    )


@given(prompt=_TEXT.filter(lambda value: len(value.strip()) >= 4))
def test_semantic_cache_key_never_contains_raw_prompt(prompt: str) -> None:
    key = build_semantic_cache_key(
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        capability_params={"prompt": prompt},
    )

    assert prompt.strip() not in key
