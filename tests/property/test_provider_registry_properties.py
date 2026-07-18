from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.providers import CredentialValidationError, create_default_registry

_URL_SAFE_SECRET = st.text(
    alphabet=string.ascii_uppercase + string.digits,
    min_size=1,
    max_size=32,
)


@given(secret=_URL_SAFE_SECRET)
def test_runpod_credential_urls_reject_userinfo_for_any_secret(secret: str) -> None:
    registry = create_default_registry()

    with pytest.raises(CredentialValidationError) as raised:
        registry.validate_credentials(
            "runpod",
            {
                "api_key": "test-key",
                "graphql_url": f"https://{secret}@api.runpod.io/graphql",
            },
        )

    assert raised.value.fields == ("graphql_url",)
    assert secret not in str(raised.value)


@given(secret=_URL_SAFE_SECRET)
def test_together_credential_urls_reject_userinfo_for_any_secret(secret: str) -> None:
    registry = create_default_registry()

    with pytest.raises(CredentialValidationError) as raised:
        registry.validate_credentials(
            "together",
            {
                "api_key": "test-key",
                "base_url": f"https://{secret}@api.together.xyz/v1",
            },
        )

    assert raised.value.fields == ("base_url",)
    assert secret not in str(raised.value)
