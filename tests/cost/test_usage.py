"""Tests for pitwall.cost.usage — token usage parsing from JSON and SSE."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pitwall.cost.usage import TokenUsage, parse_usage_json, parse_usage_sse

# ---------------------------------------------------------------------------
# parse_usage_json — happy path
# ---------------------------------------------------------------------------


class TestParseUsageJson:
    def test_standard_openai_usage(self) -> None:
        body = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            },
        }
        result = parse_usage_json(body)
        assert result == TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30)

    def test_missing_total_tokens_computed(self) -> None:
        body = {
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
        result = parse_usage_json(body)
        assert result is not None
        assert result.prompt_tokens == 5
        assert result.completion_tokens == 7
        assert result.total_tokens == 12

    def test_missing_prompt_tokens_defaults_zero(self) -> None:
        body = {
            "usage": {"completion_tokens": 15, "total_tokens": 15},
        }
        result = parse_usage_json(body)
        assert result is not None
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 15
        assert result.total_tokens == 15

    def test_missing_completion_tokens_defaults_zero(self) -> None:
        body = {
            "usage": {"prompt_tokens": 8, "total_tokens": 8},
        }
        result = parse_usage_json(body)
        assert result is not None
        assert result.prompt_tokens == 8
        assert result.completion_tokens == 0
        assert result.total_tokens == 8

    def test_string_token_values_coerced(self) -> None:
        body = {
            "usage": {
                "prompt_tokens": "100",
                "completion_tokens": "200",
                "total_tokens": "300",
            },
        }
        result = parse_usage_json(body)
        assert result == TokenUsage(prompt_tokens=100, completion_tokens=200, total_tokens=300)

    def test_integral_float_and_decimal_token_values_coerced(self) -> None:
        body = {
            "usage": {
                "prompt_tokens": 100.0,
                "completion_tokens": Decimal("200"),
                "total_tokens": Decimal("300.0"),
            },
        }
        result = parse_usage_json(body)
        assert result == TokenUsage(prompt_tokens=100, completion_tokens=200, total_tokens=300)

    def test_zero_tokens(self) -> None:
        body = {
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        result = parse_usage_json(body)
        assert result == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    def test_zero_float_and_decimal_tokens(self) -> None:
        body = {
            "usage": {
                "prompt_tokens": 0.0,
                "completion_tokens": Decimal("0"),
                "total_tokens": Decimal("0.0"),
            },
        }
        result = parse_usage_json(body)
        assert result == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    # ---------------------------------------------------------------------------
    # parse_usage_json — missing / invalid usage
    # ---------------------------------------------------------------------------

    def test_no_usage_key(self) -> None:
        assert parse_usage_json({"id": "chatcmpl-1", "choices": []}) is None

    def test_usage_none(self) -> None:
        assert parse_usage_json({"usage": None}) is None

    def test_usage_empty_dict(self) -> None:
        assert parse_usage_json({"usage": {}}) is None

    def test_usage_both_prompt_and_completion_missing(self) -> None:
        assert parse_usage_json({"usage": {"total_tokens": 50}}) is None

    def test_usage_non_dict(self) -> None:
        assert parse_usage_json({"usage": "not a dict"}) is None

    def test_usage_list(self) -> None:
        assert parse_usage_json({"usage": [1, 2, 3]}) is None

    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            "",
            -1,
            1.5,
            float("inf"),
            float("nan"),
            Decimal("-1"),
            Decimal("2.5"),
            Decimal("NaN"),
            Decimal("Infinity"),
            "-1",
            "1.5",
            "1e3",
            "１２",
            "abc",
            object(),
        ],
        ids=[
            "true",
            "false",
            "empty_string",
            "negative_int",
            "fractional_float",
            "infinite_float",
            "nan_float",
            "negative_decimal",
            "fractional_decimal",
            "nan_decimal",
            "infinite_decimal",
            "negative_string",
            "fractional_string",
            "exponent_string",
            "non_ascii_decimal_string",
            "text_string",
            "object",
        ],
    )
    def test_usage_rejects_invalid_json_token_counts(self, value: object) -> None:
        body = {
            "usage": {
                "prompt_tokens": value,
                "completion_tokens": 3,
                "total_tokens": 4,
            }
        }

        assert parse_usage_json(body) is None

    @pytest.mark.parametrize(
        "field",
        ["prompt_tokens", "completion_tokens", "total_tokens"],
    )
    def test_usage_rejects_invalid_json_token_count_fields(self, field: str) -> None:
        usage = {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
        }
        usage[field] = "not-unsigned-decimal"

        assert parse_usage_json({"usage": usage}) is None


# ---------------------------------------------------------------------------
# parse_usage_sse — happy path
# ---------------------------------------------------------------------------


class TestParseUsageSse:
    def test_single_usage_frame(self) -> None:
        sse = (
            b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
            b"data: [DONE]\n\n"
        )
        result = parse_usage_sse(sse)
        assert result == TokenUsage(prompt_tokens=3, completion_tokens=1, total_tokens=4)

    def test_last_usage_frame_wins(self) -> None:
        sse = (
            b'data: {"choices":[{"delta":{"content":"A"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
            b"data: [DONE]\n\n"
        )
        result = parse_usage_sse(sse)
        assert result == TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8)

    def test_no_usage_in_any_frame(self) -> None:
        sse = (
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        assert parse_usage_sse(sse) is None

    def test_empty_stream(self) -> None:
        assert parse_usage_sse(b"") is None

    def test_only_done(self) -> None:
        assert parse_usage_sse(b"data: [DONE]\n\n") is None

    def test_string_input(self) -> None:
        sse = (
            'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        )
        result = parse_usage_sse(sse)
        assert result == TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)

    def test_malformed_json_frame_skipped(self) -> None:
        sse = (
            b"data: {not valid json}\n\n"
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":4,"total_tokens":11}}\n\n'
            b"data: [DONE]\n\n"
        )
        result = parse_usage_sse(sse)
        assert result == TokenUsage(prompt_tokens=7, completion_tokens=4, total_tokens=11)

    def test_invalid_utf8_frame_replaced_and_skipped(self) -> None:
        sse = (
            b"data: {\xff}\n\n"
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":4,"total_tokens":11}}\n\n'
            b"data: [DONE]\n\n"
        )
        result = parse_usage_sse(sse)
        assert result == TokenUsage(prompt_tokens=7, completion_tokens=4, total_tokens=11)

    def test_frames_after_done_ignored(self) -> None:
        sse = (
            b"data: [DONE]\n\n"
            b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        )
        assert parse_usage_sse(sse) is None

    def test_real_openai_stream_options_usage(self) -> None:
        sse = (
            b'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
            b'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}\n\n'
            b"data: [DONE]\n\n"
        )
        result = parse_usage_sse(sse)
        assert result == TokenUsage(prompt_tokens=12, completion_tokens=3, total_tokens=15)

    def test_missing_total_in_sse_frame(self) -> None:
        sse = (
            b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":6}}\n\n'
            b"data: [DONE]\n\n"
        )
        result = parse_usage_sse(sse)
        assert result is not None
        assert result.prompt_tokens == 4
        assert result.completion_tokens == 6
        assert result.total_tokens == 10

    @pytest.mark.parametrize(
        "raw_usage",
        [
            '{"prompt_tokens":true,"completion_tokens":3,"total_tokens":4}',
            '{"prompt_tokens":-1,"completion_tokens":3,"total_tokens":4}',
            '{"prompt_tokens":1.5,"completion_tokens":3,"total_tokens":4}',
            '{"prompt_tokens":"1.5","completion_tokens":3,"total_tokens":4}',
            '{"prompt_tokens":"abc","completion_tokens":3,"total_tokens":4}',
        ],
        ids=[
            "bool",
            "negative",
            "fractional_number",
            "fractional_string",
            "text_string",
        ],
    )
    def test_usage_rejects_invalid_sse_token_counts(self, raw_usage: str) -> None:
        sse = f'data: {{"choices":[],"usage":{raw_usage}}}\n\ndata: [DONE]\n\n'

        assert parse_usage_sse(sse) is None


# ---------------------------------------------------------------------------
# TokenUsage frozen dataclass
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_frozen(self) -> None:
        usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        with pytest.raises(AttributeError):
            usage.prompt_tokens = 99  # type: ignore[misc]  # reason: frozen dataclass: assignment intentionally rejected

    def test_equality(self) -> None:
        a = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        b = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        assert a == b
