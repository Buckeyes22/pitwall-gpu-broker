"""Hermetic tests for the Wave-2 provider feasibility analysis structures.

These tests validate that the typed feasibility data structures in
_wave2_feasibility.py are well-formed, self-consistent, and complete.
No live network or database access is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from pitwall.providers._wave2_feasibility import (
    COREWEAVE,
    COREWEAVE_AUTH,
    COREWEAVE_INTERFACE,
    COREWEAVE_PRICING,
    MODAL,
    MODAL_AUTH,
    MODAL_INTERFACE,
    MODAL_PRICING,
    PAPERSPACE,
    PAPERSPACE_AUTH,
    PAPERSPACE_INTERFACE,
    PAPERSPACE_PRICING,
    AuthFit,
    AuthType,
    EffortRating,
    FitRating,
    InterfaceFit,
    LeaseModel,
    PricingAlignment,
    PricingFit,
    all_candidates,
    candidate_by_id,
)


class TestWave2CandidatesPresent:
    def test_all_candidates_returns_three_entries(self) -> None:
        candidates = all_candidates()
        assert len(candidates) == 3

    def test_paperspace_in_candidates(self) -> None:
        assert PAPERSPACE.candidate_id == "paperspace"
        assert PAPERSPACE.name == "Paperspace"

    def test_modal_in_candidates(self) -> None:
        assert MODAL.candidate_id == "modal"
        assert MODAL.name == "Modal"

    def test_coreweave_in_candidates(self) -> None:
        assert COREWEAVE.candidate_id == "coreweave"
        assert COREWEAVE.name == "CoreWeave"

    def test_candidate_by_id_returns_paperspace(self) -> None:
        result = candidate_by_id("paperspace")
        assert result is PAPERSPACE

    def test_candidate_by_id_returns_modal(self) -> None:
        result = candidate_by_id("modal")
        assert result is MODAL

    def test_candidate_by_id_returns_coreweave(self) -> None:
        result = candidate_by_id("coreweave")
        assert result is COREWEAVE

    def test_candidate_by_id_returns_none_for_unknown(self) -> None:
        assert candidate_by_id("unknown_provider") is None
        assert candidate_by_id("") is None


class TestCandidateFieldStructure:
    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_candidate_has_non_empty_id(self, candidate: Any) -> None:
        assert candidate.candidate_id
        assert isinstance(candidate.candidate_id, str)

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_candidate_has_non_empty_name(self, candidate: Any) -> None:
        assert candidate.name
        assert isinstance(candidate.name, str)

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_candidate_has_valid_url(self, candidate: Any) -> None:
        assert candidate.url
        assert candidate.url.startswith("https://")

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_candidate_has_non_empty_summary(self, candidate: Any) -> None:
        assert candidate.summary
        assert isinstance(candidate.summary, str)
        assert len(candidate.summary) > 20

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_candidate_blocking_issues_is_tuple(self, candidate: Any) -> None:
        assert isinstance(candidate.blocking_issues, tuple)

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_candidate_blocking_issues_all_non_empty(self, candidate: Any) -> None:
        assert len(candidate.blocking_issues) >= 1
        for issue in candidate.blocking_issues:
            assert isinstance(issue, str)
            assert issue.strip()


class TestAuthFit:
    @pytest.mark.parametrize(
        "auth_fit,expected_type",
        [
            (PAPERSPACE_AUTH, AuthType.HEADER_BEARER),
            (MODAL_AUTH, AuthType.HEADER_BEARER),
            (COREWEAVE_AUTH, AuthType.HEADER_BEARER),
        ],
    )
    def test_all_auth_fits_are_header_bearer(
        self, auth_fit: AuthFit, expected_type: AuthType
    ) -> None:
        assert auth_fit.auth_type == expected_type
        assert auth_fit.header_bearer_compatible is True

    @pytest.mark.parametrize("auth_fit", [PAPERSPACE_AUTH, MODAL_AUTH, COREWEAVE_AUTH])
    def test_auth_fit_notes_non_empty(self, auth_fit: AuthFit) -> None:
        assert auth_fit.notes
        assert isinstance(auth_fit.notes, str)
        assert len(auth_fit.notes) > 10


class TestPricingFit:
    @pytest.mark.parametrize(
        "pricing_fit,expected_alignment,min_kinds",
        [
            (PAPERSPACE_PRICING, PricingAlignment.CONVERSION_REQUIRED, 1),
            (MODAL_PRICING, PricingAlignment.CONVERSION_REQUIRED, 2),
            (COREWEAVE_PRICING, PricingAlignment.DIRECT, 1),
        ],
    )
    def test_pricing_alignment(
        self,
        pricing_fit: PricingFit,
        expected_alignment: PricingAlignment,
        min_kinds: int,
    ) -> None:
        assert pricing_fit.alignment == expected_alignment
        assert len(pricing_fit.compatible_kinds) >= min_kinds

    @pytest.mark.parametrize(
        "pricing_fit",
        [PAPERSPACE_PRICING, MODAL_PRICING, COREWEAVE_PRICING],
    )
    def test_pricing_fit_notes_non_empty(self, pricing_fit: PricingFit) -> None:
        assert pricing_fit.notes
        assert isinstance(pricing_fit.notes, str)


class TestLeaseModel:
    def test_paperspace_lease_model(self) -> None:
        assert PAPERSPACE.lease_model == LeaseModel.GPU_LEASE_SECOND

    def test_modal_lease_model(self) -> None:
        assert MODAL.lease_model == LeaseModel.SERVERLESS_INVOCATION

    def test_coreweave_lease_model(self) -> None:
        assert COREWEAVE.lease_model == LeaseModel.KUBERNETES_POD


class TestInterfaceFit:
    @pytest.mark.parametrize(
        "interface_fit", [PAPERSPACE_INTERFACE, MODAL_INTERFACE, COREWEAVE_INTERFACE]
    )
    def test_interface_fit_has_all_gap_fields(self, interface_fit: InterfaceFit) -> None:
        assert isinstance(interface_fit.provision_gaps, tuple)
        assert isinstance(interface_fit.status_gaps, tuple)
        assert isinstance(interface_fit.reconcile_gaps, tuple)
        assert isinstance(interface_fit.teardown_gaps, tuple)
        assert isinstance(interface_fit.notes, str)

    @pytest.mark.parametrize(
        "interface_fit", [PAPERSPACE_INTERFACE, MODAL_INTERFACE, COREWEAVE_INTERFACE]
    )
    def test_interface_fit_gaps_all_non_empty(self, interface_fit: InterfaceFit) -> None:
        for gap_list in (
            interface_fit.provision_gaps,
            interface_fit.status_gaps,
            interface_fit.reconcile_gaps,
            interface_fit.teardown_gaps,
        ):
            assert len(gap_list) >= 1
            for gap in gap_list:
                assert isinstance(gap, str)
                assert gap.strip()

    @pytest.mark.parametrize(
        "interface_fit", [PAPERSPACE_INTERFACE, MODAL_INTERFACE, COREWEAVE_INTERFACE]
    )
    def test_interface_fit_notes_non_empty(self, interface_fit: InterfaceFit) -> None:
        assert interface_fit.notes
        assert len(interface_fit.notes) > 10


class TestRatings:
    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_effort_rating_is_valid_enum(self, candidate: Any) -> None:
        assert isinstance(candidate.effort_rating, EffortRating)
        EffortRating(candidate.effort_rating.value)

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_fit_rating_is_valid_enum(self, candidate: Any) -> None:
        assert isinstance(candidate.fit_rating, FitRating)
        FitRating(candidate.fit_rating.value)

    def test_effort_ratings_span_across_scale(self) -> None:
        ratings = {c.effort_rating for c in all_candidates()}
        assert len(ratings) >= 2

    def test_fit_ratings_span_across_scale(self) -> None:
        ratings = {c.fit_rating for c in all_candidates()}
        assert len(ratings) >= 2


class TestPaperspaceSpecific:
    def test_paperspace_blocking_issues_exist(self) -> None:
        assert len(PAPERSPACE.blocking_issues) >= 1

    def test_paperspace_pricing_supports_per_second(self) -> None:
        assert "per_second" in PAPERSPACE_PRICING.compatible_kinds
        assert "per_vm_second" in PAPERSPACE_PRICING.compatible_kinds

    def test_paperspace_auth_bearer_compatible(self) -> None:
        assert PAPERSPACE_AUTH.header_bearer_compatible is True
        assert PAPERSPACE_AUTH.auth_type == AuthType.HEADER_BEARER


class TestModalSpecific:
    def test_modal_blocking_issues_exist(self) -> None:
        assert len(MODAL.blocking_issues) >= 1

    def test_modal_pricing_supports_multiple_kinds(self) -> None:
        kinds = MODAL_PRICING.compatible_kinds
        assert "per_second" in kinds
        assert "per_request" in kinds

    def test_modal_lease_model_is_serverless(self) -> None:
        assert MODAL.lease_model == LeaseModel.SERVERLESS_INVOCATION


class TestCoreWeaveSpecific:
    def test_coreweave_blocking_issues_exist(self) -> None:
        assert len(COREWEAVE.blocking_issues) >= 1

    def test_coreweave_pricing_is_direct(self) -> None:
        assert COREWEAVE_PRICING.alignment == PricingAlignment.DIRECT

    def test_coreweave_lease_model_is_kubernetes(self) -> None:
        assert COREWEAVE.lease_model == LeaseModel.KUBERNETES_POD

    def test_coreweave_auth_bearer_compatible(self) -> None:
        assert COREWEAVE_AUTH.header_bearer_compatible is True
        assert COREWEAVE_AUTH.auth_type == AuthType.HEADER_BEARER


class TestSelfConsistency:
    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_candidate_id_in_assessment_matches_lookup(self, candidate: Any) -> None:
        looked_up = candidate_by_id(candidate.candidate_id)
        assert looked_up is candidate

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_assessment_has_reasonable_summary_length(self, candidate: Any) -> None:
        assert 50 <= len(candidate.summary) <= 1000

    @pytest.mark.parametrize("candidate", [PAPERSPACE, MODAL, COREWEAVE])
    def test_no_null_fields_in_assessment(self, candidate: Any) -> None:
        for field_name in dir(candidate):
            if field_name.startswith("_"):
                continue
            value = getattr(candidate, field_name, None)
            assert value is not None, f"Field {field_name} should not be None"
