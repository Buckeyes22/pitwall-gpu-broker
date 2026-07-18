"""Release-tier tests for the BGE-M3 smoke envelope.

Tier: smoke
Purpose: Validate live BGE-M3 endpoint exit criteria.

The smoke envelope is the second tier of the 4-tier pre-spend validation:
  1. Cost-gate dry-run — routing + cost estimation, no RunPod call
  2. One-node smoke envelope — real POST /v1/inference against BGE-M3,
     confirms end-to-end request path including Langfuse trace emission
  3. Kill drill — kill-switch drops workers <30s, kill_log row created
  4. Sovereignty refuse — homelab_only workloads never dispatch to cloud

Exit criteria:
  - Response contains valid dense embeddings
  - Cost ceiling not exceeded (no 402 BudgetRejected)
  - X-Pitwall-Trace header present (Langfuse trace emitted)
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest

BGE_M3_SMOKE_CAPABILITY = "embedding.bge-m3"
BGE_M3_SMOKE_TEXT = "hello world"
BGE_M3_SMOKE_COST_CEILING_USD = 0.001
BGE_M3_DENSE_DIM = 1024
# Must exceed the server-side ServerlessLBClient budget (330s) so the test
# observes the server's verdict on a cold endpoint instead of racing it.
SMOKE_CLIENT_TIMEOUT_S = 360.0


def _is_live() -> bool:
    try:
        from pitwall.live import is_live as _is_live
    except ImportError:
        return False
    return _is_live()


@pytest.mark.release
@pytest.mark.live
class TestBGEM3SmokeEnvelope:
    """Live smoke tests for BGE-M3 capability via Pitwall /v1/inference.

    These tests are gated behind RUNPOD_LIVE=1 and PITWALL_BASE_URL.
    They make real HTTP calls to the running Pitwall server and assert
    the three smoke envelope exit criteria:
      1. Response contains valid dense embeddings
      2. Cost ceiling not exceeded (no 402)
      3. X-Pitwall-Trace header is present
    """

    @pytest.fixture(autouse=True)
    def require_live_env(self) -> None:
        """Skip if RUNPOD_LIVE=1 or PITWALL_BASE_URL is not set."""
        if not _is_live():
            pytest.skip("live smoke test requires RUNPOD_LIVE=1 and PITWALL_BASE_URL to be set")
        if not os.environ.get("RUNPOD_API_KEY"):
            pytest.skip("live smoke test requires RUNPOD_API_KEY environment variable")

    def _base_url(self) -> str:
        base = os.environ.get("PITWALL_BASE_URL", "").rstrip("/")
        if not base:
            pytest.fail("PITWALL_BASE_URL is not set")
        return base

    def _api_key(self) -> str:
        key = os.environ.get("RUNPOD_API_KEY", "")
        if not key:
            pytest.fail("RUNPOD_API_KEY is not set")
        return key

    def test_smoke_envelope_response_has_dense_embeddings(self) -> None:
        """Assert POST /v1/inference returns dense embeddings for BGE-M3.

        Exit criterion 1: Response contains valid dense embeddings.
        The dense vector dimension for BGE-M3 is 1024.
        """
        base_url = self._base_url()
        api_key = self._api_key()

        with httpx.Client(base_url=base_url, timeout=SMOKE_CLIENT_TIMEOUT_S) as client:
            response = client.post(
                "/v1/inference",
                json={
                    "capability": BGE_M3_SMOKE_CAPABILITY,
                    "texts": [BGE_M3_SMOKE_TEXT],
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )

        body = response.json()
        result = body.get("result", {})

        assert "dense" in result, f"result must contain 'dense' key: {result}"
        dense = result["dense"]
        assert isinstance(dense, list), f"dense must be a list, got {type(dense)}"
        assert len(dense) == 1, f"expected 1 embedding, got {len(dense)}"
        assert isinstance(dense[0], list), (
            f"dense[0] must be a list of floats, got {type(dense[0])}"
        )
        assert len(dense[0]) == BGE_M3_DENSE_DIM, (
            f"BGE-M3 dense dimension must be {BGE_M3_DENSE_DIM}, got {len(dense[0])}"
        )

        for i, val in enumerate(dense[0]):
            assert isinstance(val, (int, float)), f"dense[0][{i}] must be a number, got {type(val)}"

    def test_smoke_envelope_cost_ceiling_not_exceeded(self) -> None:
        """Assert POST /v1/inference does not return 402 BudgetRejected.

        Exit criterion 2: Cost ceiling not exceeded.
        A BGE-M3 query embedding costs approximately $0.0001, well under
        the default per_request_max_usd of $10.00.
        """
        base_url = self._base_url()
        api_key = self._api_key()

        with httpx.Client(base_url=base_url, timeout=SMOKE_CLIENT_TIMEOUT_S) as client:
            response = client.post(
                "/v1/inference",
                json={
                    "capability": BGE_M3_SMOKE_CAPABILITY,
                    "texts": [BGE_M3_SMOKE_TEXT],
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )

        if response.status_code == 402:
            body = response.json()
            pytest.fail(f"Budget ceiling exceeded (402): {body}")

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )

    def test_smoke_envelope_langfuse_trace_header_present(self) -> None:
        """Assert POST /v1/inference returns X-Pitwall-Trace header.

        Exit criterion 3: Langfuse trace id present.
        When Langfuse is configured, the server emits X-Pitwall-Trace header
        with the trace id from the Langfuse API response.
        """
        base_url = self._base_url()
        api_key = self._api_key()

        with httpx.Client(base_url=base_url, timeout=SMOKE_CLIENT_TIMEOUT_S) as client:
            response = client.post(
                "/v1/inference",
                json={
                    "capability": BGE_M3_SMOKE_CAPABILITY,
                    "texts": [BGE_M3_SMOKE_TEXT],
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )

        trace_id = response.headers.get("X-Pitwall-Trace")
        assert trace_id is not None, (
            "X-Pitwall-Trace header must be present when Langfuse is configured. "
            f"Response headers: {dict(response.headers)}"
        )
        assert isinstance(trace_id, str), f"X-Pitwall-Trace must be a string, got {type(trace_id)}"
        assert len(trace_id) > 0, "X-Pitwall-Trace must not be empty"

    def test_smoke_envelope_all_criteria_together(self) -> None:
        """Assert all three smoke envelope exit criteria in a single request.

        This test validates all three exit criteria with a single live call
        to minimize API usage and cost during smoke testing.
        """
        base_url = self._base_url()
        api_key = self._api_key()

        with httpx.Client(base_url=base_url, timeout=SMOKE_CLIENT_TIMEOUT_S) as client:
            response = client.post(
                "/v1/inference",
                json={
                    "capability": BGE_M3_SMOKE_CAPABILITY,
                    "texts": [BGE_M3_SMOKE_TEXT],
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )

        body = response.json()
        result = body.get("result", {})

        assert "dense" in result, f"result must contain 'dense' key: {result}"
        dense = result["dense"]
        assert isinstance(dense, list) and len(dense) == 1, (
            f"dense must be a list with 1 embedding, got {dense}"
        )
        assert len(dense[0]) == BGE_M3_DENSE_DIM, (
            f"BGE-M3 dense dimension must be {BGE_M3_DENSE_DIM}"
        )

        trace_id = response.headers.get("X-Pitwall-Trace")
        assert trace_id is not None and len(trace_id) > 0, (
            "X-Pitwall-Trace header must be present and non-empty"
        )

        workload_id = response.headers.get("X-Pitwall-Workload-ID")
        assert workload_id is not None and len(workload_id) > 0, (
            "X-Pitwall-Workload-ID header must be present and non-empty"
        )


@pytest.mark.release
def test_bge_m3_smoke_envelope_is_live_release_marked() -> None:
    markers = {mark.name for mark in TestBGEM3SmokeEnvelope.pytestmark}

    assert {"live", "release"} <= markers


if __name__ == "__main__":
    if not _is_live():
        print(
            "[test_smoke_tier] SKIP: live test requires RUNPOD_LIVE=1 and PITWALL_BASE_URL",
            file=sys.stderr,
        )
        sys.exit(0)

    print("[test_smoke_tier] Running BGE-M3 smoke envelope tests...")
    sys.exit(pytest.main([__file__, "-v", "-m", "release"]))
