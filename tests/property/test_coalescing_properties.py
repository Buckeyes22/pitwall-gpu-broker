"""Property tests for inference coalescing keys."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.routing.coalescing import build_inference_coalescing_key

pytestmark = pytest.mark.property

_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters=("\x00",),
    ),
    max_size=24,
)
_JSON_SCALAR = st.none() | st.booleans() | st.integers() | _TEXT
_JSON_VALUE = st.recursive(
    _JSON_SCALAR,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(_TEXT.filter(bool), children, max_size=4)
    ),
    max_leaves=12,
)
_PARAMS = st.dictionaries(_TEXT.filter(bool), _JSON_VALUE, max_size=5)


@given(params=_PARAMS)
def test_inference_coalescing_key_is_stable_for_dict_order(
    params: dict[str, object],
) -> None:
    reordered = dict(reversed(tuple(params.items())))

    assert build_inference_coalescing_key(
        idempotency_key=None,
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        capability_params=params,
    ) == build_inference_coalescing_key(
        idempotency_key=None,
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        capability_params=reordered,
    )


@given(params=_PARAMS, idempotency_key=_TEXT.filter(bool))
def test_idempotency_key_scope_is_distinct_from_anonymous_content_hash(
    params: dict[str, object],
    idempotency_key: str,
) -> None:
    assert build_inference_coalescing_key(
        idempotency_key=idempotency_key,
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        capability_params=params,
    ) != build_inference_coalescing_key(
        idempotency_key=None,
        capability_id="cap_bge_m3",
        provider_id="prov_bge_m3",
        capability_params=params,
    )
