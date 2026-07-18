"""Hermetic unit tests for ``pitwall.cost.billing_read``.

All network I/O is faked via ``httpx.MockTransport``; no live RunPod calls.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pitwall.cost.billing_read import (
    BillingSnapshot,
    BudgetGateLike,
    BudgetReconciliation,
    read_billing_snapshot,
    reconcile_with_budget,
)
from pitwall.runpod_client.graphql import (
    RunpodCreditsBalance,
    RunpodGraphQLError,
    RunpodGraphQLHTTPError,
    RunpodGraphQLResponseError,
)

pytestmark = pytest.mark.anyio

GraphQLHandler = Callable[[httpx.Request], httpx.Response]


@dataclass
class _GraphQLFake:
    responses: list[httpx.Response | GraphQLHandler] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, response: httpx.Response | GraphQLHandler) -> None:
        self.responses.append(response)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected GraphQL request: {request.method} {request.url}")
        response = self.responses.pop(0)
        if callable(response):
            return response(request)
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
            extensions=response.extensions,
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _graphql_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": "application/json"},
    )


def _request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


class TestBillingSnapshot:
    def test_from_runpod_model_maps_all_fields(self) -> None:
        balance = RunpodCreditsBalance(
            user_id="user-42",
            client_balance=Decimal("123.456789"),
            current_spend_per_hr=Decimal("1.234567"),
            spend_limit=Decimal("500.000000"),
            min_balance=Decimal("10.000000"),
            under_balance=False,
        )
        snapshot = BillingSnapshot.from_runpod(balance)

        assert snapshot.user_id == "user-42"
        assert snapshot.client_balance_usd == Decimal("123.456789")
        assert snapshot.current_spend_per_hr_usd == Decimal("1.234567")
        assert snapshot.spend_limit_usd == Decimal("500.000000")
        assert snapshot.min_balance_usd == Decimal("10.000000")
        assert snapshot.under_balance is False

    def test_from_runpod_model_allows_optional_none(self) -> None:
        balance = RunpodCreditsBalance(
            user_id=None,
            client_balance=Decimal("0"),
            current_spend_per_hr=None,
            spend_limit=None,
            min_balance=None,
            under_balance=True,
        )
        snapshot = BillingSnapshot.from_runpod(balance)

        assert snapshot.user_id is None
        assert snapshot.client_balance_usd == Decimal("0")
        assert snapshot.current_spend_per_hr_usd is None
        assert snapshot.spend_limit_usd is None
        assert snapshot.min_balance_usd is None
        assert snapshot.under_balance is True

    def test_to_serializable_dict_converts_decimals_to_strings(self) -> None:
        snapshot = BillingSnapshot(
            user_id="user-1",
            client_balance_usd=Decimal("42.250000"),
            current_spend_per_hr_usd=Decimal("1.125000"),
            spend_limit_usd=Decimal("100.000000"),
            min_balance_usd=Decimal("5.000000"),
            under_balance=False,
        )
        d = snapshot.to_serializable_dict()

        assert d == {
            "user_id": "user-1",
            "client_balance_usd": "42.250000",
            "current_spend_per_hr_usd": "1.125000",
            "spend_limit_usd": "100.000000",
            "min_balance_usd": "5.000000",
            "under_balance": False,
        }

    def test_to_serializable_dict_handles_none_fields(self) -> None:
        snapshot = BillingSnapshot(
            user_id=None,
            client_balance_usd=Decimal("0"),
            current_spend_per_hr_usd=None,
            spend_limit_usd=None,
            min_balance_usd=None,
            under_balance=True,
        )
        d = snapshot.to_serializable_dict()

        assert d["current_spend_per_hr_usd"] is None
        assert d["spend_limit_usd"] is None
        assert d["min_balance_usd"] is None
        assert d["under_balance"] is True

    @pytest.mark.parametrize(
        "raw_balance",
        [
            Decimal("42.250000000000000001"),
            Decimal("0"),
            Decimal("1000000.999999"),
        ],
    )
    def test_decimal_precision_preserved(self, raw_balance: Decimal) -> None:
        balance = RunpodCreditsBalance(
            user_id="u",
            client_balance=raw_balance,
            current_spend_per_hr=None,
            spend_limit=None,
            min_balance=None,
            under_balance=False,
        )
        snapshot = BillingSnapshot.from_runpod(balance)
        assert snapshot.client_balance_usd == raw_balance
        assert isinstance(snapshot.client_balance_usd, Decimal)


class TestReadBillingSnapshot:
    async def test_happy_path_returns_typed_snapshot(self) -> None:
        fake = _GraphQLFake()
        fake.add(
            _graphql_response(
                """
                {
                  "data": {
                    "myself": {
                      "id": "user-1",
                      "clientBalance": 42.250000000000000001,
                      "currentSpendPerHr": 1.125000000000000001,
                      "spendLimit": 100.000000000000000001,
                      "minBalance": 5.000000000000000001,
                      "underBalance": false
                    }
                  }
                }
                """
            )
        )
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

        snapshot = await read_billing_snapshot(client)
        await client.aclose()

        assert snapshot.user_id == "user-1"
        assert snapshot.client_balance_usd == Decimal("42.250000000000000001")
        assert snapshot.current_spend_per_hr_usd == Decimal("1.125000000000000001")
        assert snapshot.spend_limit_usd == Decimal("100.000000000000000001")
        assert snapshot.min_balance_usd == Decimal("5.000000000000000001")
        assert snapshot.under_balance is False

        body = _request_body(fake.requests[0])
        assert "query" in body
        assert "clientBalance" in body["query"]

    async def test_propagates_graphql_error_envelope(self) -> None:
        fake = _GraphQLFake()
        fake.add(
            _graphql_response(
                """
                {
                  "errors": [
                    {"message": "unauthorized", "path": ["myself"]}
                  ],
                  "data": {"myself": null}
                }
                """
            )
        )
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        client = RunpodGraphQLClient(api_key="bad-key", transport=fake.transport())

        with pytest.raises(RunpodGraphQLError) as exc_info:
            await read_billing_snapshot(client)
        await client.aclose()

        assert "unauthorized" in str(exc_info.value)

    async def test_propagates_http_error(self) -> None:
        fake = _GraphQLFake()
        fake.add(httpx.Response(503, text="Service Unavailable"))
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

        with pytest.raises(RunpodGraphQLHTTPError) as exc_info:
            await read_billing_snapshot(client)
        await client.aclose()

        assert exc_info.value.status_code == 503

    async def test_propagates_malformed_response(self) -> None:
        fake = _GraphQLFake()
        fake.add(_graphql_response('{"data": null}'))
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

        with pytest.raises(RunpodGraphQLResponseError):
            await read_billing_snapshot(client)
        await client.aclose()


class _FakeBudgetGate:
    """Minimal fake satisfying BudgetGateLike."""

    def __init__(
        self,
        monthly_budget_usd: Decimal,
        mtd_spend: Decimal,
    ) -> None:
        self.monthly_budget_usd = monthly_budget_usd
        self._mtd_spend = mtd_spend

    async def current_mtd_spend(self) -> Decimal:
        return self._mtd_spend


class TestReconcileWithBudget:
    async def test_happy_path_computes_variance(self) -> None:
        fake = _GraphQLFake()
        fake.add(
            _graphql_response(
                """
                {
                  "data": {
                    "myself": {
                      "id": "user-1",
                      "clientBalance": 500.000000,
                      "currentSpendPerHr": 2.500000,
                      "spendLimit": 1000.000000,
                      "minBalance": 50.000000,
                      "underBalance": false
                    }
                  }
                }
                """
            )
        )
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())
        gate = _FakeBudgetGate(
            monthly_budget_usd=Decimal("1000.000000"),
            mtd_spend=Decimal("400.000000"),
        )

        rec = await reconcile_with_budget(client, gate)
        await client.aclose()

        assert rec.runpod_balance_usd == Decimal("500.000000")
        assert rec.runpod_spend_per_hr_usd == Decimal("2.500000")
        assert rec.runpod_spend_limit_usd == Decimal("1000.000000")
        assert rec.runpod_under_balance is False
        assert rec.pitwall_mtd_spend_usd == Decimal("400.000000")
        assert rec.pitwall_monthly_budget_usd == Decimal("1000.000000")
        assert rec.pitwall_budget_remaining_usd == Decimal("600.000000")
        assert rec.variance_usd == Decimal("-100.000000")

    async def test_zero_remaining_when_mtd_exceeds_budget(self) -> None:
        fake = _GraphQLFake()
        fake.add(
            _graphql_response(
                """
                {
                  "data": {
                    "myself": {
                      "id": "user-1",
                      "clientBalance": 100.000000,
                      "currentSpendPerHr": 5.000000,
                      "spendLimit": 500.000000,
                      "minBalance": 10.000000,
                      "underBalance": true
                    }
                  }
                }
                """
            )
        )
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())
        gate = _FakeBudgetGate(
            monthly_budget_usd=Decimal("500.000000"),
            mtd_spend=Decimal("550.000000"),
        )

        rec = await reconcile_with_budget(client, gate)
        await client.aclose()

        assert rec.pitwall_budget_remaining_usd == Decimal("0")
        assert rec.variance_usd == Decimal("100.000000")
        assert rec.runpod_under_balance is True

    async def test_negative_variance_when_runpod_balance_low(self) -> None:
        fake = _GraphQLFake()
        fake.add(
            _graphql_response(
                """
                {
                  "data": {
                    "myself": {
                      "id": "user-1",
                      "clientBalance": 50.000000,
                      "currentSpendPerHr": 10.000000,
                      "spendLimit": null,
                      "minBalance": null,
                      "underBalance": false
                    }
                  }
                }
                """
            )
        )
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())
        gate = _FakeBudgetGate(
            monthly_budget_usd=Decimal("1000.000000"),
            mtd_spend=Decimal("100.000000"),
        )

        rec = await reconcile_with_budget(client, gate)
        await client.aclose()

        assert rec.runpod_balance_usd == Decimal("50.000000")
        assert rec.runpod_spend_limit_usd is None
        assert rec.runpod_spend_per_hr_usd == Decimal("10.000000")
        assert rec.pitwall_budget_remaining_usd == Decimal("900.000000")
        assert rec.variance_usd == Decimal("-850.000000")

    async def test_protocol_check_on_budget_gate(self) -> None:
        gate = _FakeBudgetGate(
            monthly_budget_usd=Decimal("100.000000"),
            mtd_spend=Decimal("0"),
        )
        assert isinstance(gate, BudgetGateLike)


class TestBudgetReconciliation:
    def test_to_serializable_dict_converts_all_decimals(self) -> None:
        rec = BudgetReconciliation(
            runpod_balance_usd=Decimal("500.000000"),
            runpod_spend_per_hr_usd=Decimal("2.500000"),
            runpod_spend_limit_usd=Decimal("1000.000000"),
            runpod_under_balance=False,
            pitwall_mtd_spend_usd=Decimal("400.000000"),
            pitwall_monthly_budget_usd=Decimal("1000.000000"),
            pitwall_budget_remaining_usd=Decimal("600.000000"),
            variance_usd=Decimal("-100.000000"),
        )
        d = rec.to_serializable_dict()

        assert d == {
            "runpod_balance_usd": "500.000000",
            "runpod_spend_per_hr_usd": "2.500000",
            "runpod_spend_limit_usd": "1000.000000",
            "runpod_under_balance": False,
            "pitwall_mtd_spend_usd": "400.000000",
            "pitwall_monthly_budget_usd": "1000.000000",
            "pitwall_budget_remaining_usd": "600.000000",
            "variance_usd": "-100.000000",
        }

    def test_to_serializable_dict_handles_none_spend_fields(self) -> None:
        rec = BudgetReconciliation(
            runpod_balance_usd=Decimal("0"),
            runpod_spend_per_hr_usd=None,
            runpod_spend_limit_usd=None,
            runpod_under_balance=True,
            pitwall_mtd_spend_usd=Decimal("0"),
            pitwall_monthly_budget_usd=Decimal("100.000000"),
            pitwall_budget_remaining_usd=Decimal("100.000000"),
            variance_usd=Decimal("-100.000000"),
        )
        d = rec.to_serializable_dict()

        assert d["runpod_spend_per_hr_usd"] is None
        assert d["runpod_spend_limit_usd"] is None
        assert d["runpod_under_balance"] is True

    def test_frozen_dataclass_is_immutable(self) -> None:
        rec = BudgetReconciliation(
            runpod_balance_usd=Decimal("1"),
            runpod_spend_per_hr_usd=None,
            runpod_spend_limit_usd=None,
            runpod_under_balance=False,
            pitwall_mtd_spend_usd=Decimal("0"),
            pitwall_monthly_budget_usd=Decimal("1"),
            pitwall_budget_remaining_usd=Decimal("1"),
            variance_usd=Decimal("0"),
        )
        with pytest.raises(AttributeError):
            rec.runpod_balance_usd = Decimal("2")  # type: ignore[misc]  # reason: frozen dataclass: assignment intentionally rejected


class TestNoFloatLeakage:
    """Property-style checks that money fields never carry float."""

    async def test_json_floats_are_parsed_as_decimal(self) -> None:
        cases = [
            '{"data":{"myself":{"id":"u","clientBalance":42.25,"currentSpendPerHr":1.125,"spendLimit":100.0,"minBalance":5.0,"underBalance":false}}}',
            '{"data":{"myself":{"id":"u","clientBalance":0,"currentSpendPerHr":0,"spendLimit":0,"minBalance":0,"underBalance":true}}}',
        ]
        from pitwall.runpod_client.graphql import RunpodGraphQLClient

        for balance_json in cases:
            fake = _GraphQLFake()
            fake.add(_graphql_response(balance_json))
            client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())
            snapshot = await read_billing_snapshot(client)
            await client.aclose()

            assert isinstance(snapshot.client_balance_usd, Decimal)
            assert not isinstance(snapshot.client_balance_usd, float)
            if snapshot.current_spend_per_hr_usd is not None:
                assert isinstance(snapshot.current_spend_per_hr_usd, Decimal)
                assert not isinstance(snapshot.current_spend_per_hr_usd, float)
