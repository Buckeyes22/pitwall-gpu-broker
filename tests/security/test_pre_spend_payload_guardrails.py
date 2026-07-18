"""Property coverage for pre-spend payload guardrail redaction."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.audit import sixteen_check

pytestmark = [pytest.mark.security, pytest.mark.property]


_SAFE_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters=["@", "\x00"],
    ),
    max_size=32,
)


@given(prefix=_SAFE_TEXT, suffix=_SAFE_TEXT)
def test_email_redaction_never_leaves_original_email_in_result(
    prefix: str,
    suffix: str,
) -> None:
    email = "ada.lovelace@example.com"
    payload = {"messages": [{"content": f"{prefix} {email} {suffix}"}]}

    result = sixteen_check.scan_pre_spend_payload(payload)

    assert result.decision == sixteen_check.PreSpendDecision.REDACT
    assert email not in str(result.to_dict())
    assert email not in str(result.redacted_payload)
    assert result.redacted_payload == {
        "messages": [{"content": f"{prefix} [REDACTED:email] {suffix}"}]
    }


@given(token=st.from_regex(re.compile(r"sk-test_[A-Za-z0-9]{32}"), fullmatch=True))
def test_secret_redaction_blocks_and_never_returns_original_token(token: str) -> None:
    result = sixteen_check.scan_pre_spend_payload({"input": f"token={token}"})

    assert result.decision == sixteen_check.PreSpendDecision.BLOCK
    assert result.blocked is True
    assert token not in str(result.to_dict())
    assert token not in str(result.redacted_payload)
    assert result.redacted_payload == {"input": "token=[REDACTED:secret]"}
