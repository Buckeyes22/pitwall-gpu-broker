"""Tests for pitwall.workers.header_policy — header policy for RunPod API requests."""

from __future__ import annotations

import pytest

from pitwall.workers.header_policy import (
    HOP_BY_HOP_HEADERS,
    apply_header_policy,
)


class TestHopByHopHeaders:
    """Tests that hop-by-hop headers are dropped."""

    @pytest.mark.parametrize(
        "header_name",
        sorted(HOP_BY_HOP_HEADERS),
    )
    def test_hop_by_hop_headers_are_dropped(self, header_name: str) -> None:
        """Each defined hop-by-hop header is removed from the output."""
        incoming = {header_name: "some-value"}
        result = apply_header_policy(incoming, "rp-key-123")
        assert header_name not in result
        assert "authorization" in result
        assert result["authorization"] == "Bearer rp-key-123"

    def test_case_insensitive_hop_by_hop_detection(self) -> None:
        """Hop-by-hop headers are detected regardless of case."""
        incoming = {
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=5",
            "Transfer-Encoding": "chunked",
            "UPGRADE": "websocket",
        }
        result = apply_header_policy(incoming, "rp-key-123")
        assert "connection" not in result
        assert "keep-alive" not in result
        assert "transfer-encoding" not in result
        assert "upgrade" not in result
        assert "authorization" in result


class TestConsumerAuthHeaders:
    """Tests that consumer-supplied Authorization headers are dropped."""

    def test_consumer_authorization_is_dropped(self) -> None:
        """Consumer Authorization header is removed to prevent auth injection."""
        incoming = {
            "authorization": "Bearer consumer-secret-token",
            "content-type": "application/json",
        }
        result = apply_header_policy(incoming, "rp-key-123")
        assert "authorization" in result
        assert result["authorization"] == "Bearer rp-key-123"
        assert "consumer-secret-token" not in str(result)

    def test_case_insensitive_consumer_auth_detection(self) -> None:
        """Consumer auth headers are detected regardless of case."""
        incoming = {
            "Authorization": "Basic dXNlcjpwYXNz",
            "content-type": "application/json",
        }
        result = apply_header_policy(incoming, "rp-key-123")
        assert "authorization" in result
        assert result["authorization"] == "Bearer rp-key-123"


class TestRunPodBearerInjection:
    """Tests that the RunPod Bearer token is correctly injected."""

    def test_bearer_token_injected(self) -> None:
        """The RunPod Bearer token is always present in output."""
        incoming: dict[str, str] = {}
        result = apply_header_policy(incoming, "rp-secret-key")
        assert result["authorization"] == "Bearer rp-secret-key"

    def test_existing_auth_replaced(self) -> None:
        """Any existing Authorization header is replaced with the RunPod token."""
        incoming = {"authorization": "Bearer old-token", "content-type": "application/json"}
        result = apply_header_policy(incoming, "new-runpod-key")
        assert result["authorization"] == "Bearer new-runpod-key"

    def test_bearer_format(self) -> None:
        """The injected token uses the correct Bearer format."""
        incoming: dict[str, str] = {}
        result = apply_header_policy(incoming, "my-api-key")
        assert result["authorization"] == "Bearer my-api-key"


class TestPassthroughHeaders:
    """Tests that non-sensitive headers are passed through."""

    def test_content_type_passed_through(self) -> None:
        """Content-Type header is preserved."""
        incoming = {"content-type": "application/json"}
        result = apply_header_policy(incoming, "rp-key")
        assert result["content-type"] == "application/json"

    def test_custom_headers_passed_through(self) -> None:
        """Custom headers are preserved."""
        incoming = {
            "x-request-id": "req-123",
            "x-custom-header": "some-value",
            "content-type": "application/json",
        }
        result = apply_header_policy(incoming, "rp-key")
        assert result["x-request-id"] == "req-123"
        assert result["x-custom-header"] == "some-value"
        assert result["content-type"] == "application/json"

    def test_case_preserved_for_passthrough_headers(self) -> None:
        """Header case is preserved for non-hop-by-hop, non-auth headers."""
        incoming = {"X-Custom-Header": "value", "Content-Type": "application/json"}
        result = apply_header_policy(incoming, "rp-key")
        assert result.get("X-Custom-Header") == "value"
        assert result.get("Content-Type") == "application/json"


class TestApplyHeaderPolicyEdgeCases:
    """Edge case tests for apply_header_policy."""

    def test_empty_incoming_headers(self) -> None:
        """Empty input headers still produces valid output with RunPod token."""
        incoming: dict[str, str] = {}
        result = apply_header_policy(incoming, "rp-key")
        assert result == {"authorization": "Bearer rp-key"}

    def test_all_hop_by_hop_removed(self) -> None:
        """When all headers are hop-by-hop, only RunPod token remains."""
        incoming = dict.fromkeys(HOP_BY_HOP_HEADERS, "value")
        incoming["x-custom"] = "custom-value"
        result = apply_header_policy(incoming, "rp-key")
        assert result == {"x-custom": "custom-value", "authorization": "Bearer rp-key"}

    def test_mixed_hop_by_hop_and_regular(self) -> None:
        """Mix of hop-by-hop and regular headers works correctly."""
        incoming = {
            "connection": "close",
            "content-type": "application/json",
            "keep-alive": "timeout=5",
            "x-custom": "value",
        }
        result = apply_header_policy(incoming, "rp-key")
        assert result == {
            "content-type": "application/json",
            "x-custom": "value",
            "authorization": "Bearer rp-key",
        }

    def test_original_dict_not_modified(self) -> None:
        """The original headers dict is not modified."""
        incoming = {"authorization": "Bearer consumer", "connection": "keep-alive"}
        original = dict(incoming)
        apply_header_policy(incoming, "rp-key")
        assert incoming == original
