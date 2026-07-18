"""Tagged cost pricing models and compatibility estimators for Pitwall admission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from pitwall.core.enums import CostMode
from pitwall.core.models import Capability

ProviderCost = Mapping[str, Any]

EstimatePayload = dict[str, Any]

_USD_QUANTUM = Decimal("0.000001")


@runtime_checkable
class PricingModelProtocol(Protocol):
    """Uniform interface implemented by every tagged pricing variant."""

    def estimate(
        self,
        capability: Capability,
        payload: EstimatePayload,
    ) -> Decimal: ...

    def upper_bound(
        self,
        capability: Capability,
        payload: EstimatePayload,
    ) -> Decimal: ...


class _PricingBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GpuHourPricing(_PricingBase):
    """Current RunPod GPU active-second pricing, tagged for future extension.

    Existing provider records store the GPU-hour-derived active rate as
    ``per_second_active``.  Keeping this field name preserves the current exact
    estimate path while making the shape explicit.
    """

    kind: Literal["gpu_hour"] = "gpu_hour"
    per_second_active: Decimal

    @field_validator("per_second_active", mode="before")
    @classmethod
    def _validate_per_second_active(cls, value: object) -> Decimal:
        return _non_negative_decimal(value, "per_second_active")

    def estimate(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        return _usd(self.per_second_active * _worst_case_seconds(capability))

    def upper_bound(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        return self.estimate(capability, payload)


class PerRequestPricing(_PricingBase):
    """Flat per-invocation pricing used by existing public endpoints."""

    kind: Literal["per_request"] = "per_request"
    per_request: Decimal

    @field_validator("per_request", mode="before")
    @classmethod
    def _validate_per_request(cls, value: object) -> Decimal:
        return _non_negative_decimal(value, "per_request")

    def estimate(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        return _usd(self.per_request)

    def upper_bound(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        return self.estimate(capability, payload)


class PerSecondPricing(_PricingBase):
    """Per-second compute pricing with an optional spot/bid ceiling."""

    kind: Literal["per_second"] = "per_second"
    rate_per_second: Decimal
    bid_rate_per_second: Decimal | None = None

    @field_validator("rate_per_second", mode="before")
    @classmethod
    def _validate_rate_per_second(cls, value: object) -> Decimal:
        return _non_negative_decimal(value, "rate_per_second")

    @field_validator("bid_rate_per_second", mode="before")
    @classmethod
    def _validate_bid_rate_per_second(cls, value: object) -> Decimal | None:
        return _optional_non_negative_decimal(value, "bid_rate_per_second")

    def estimate(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        return _usd(self.rate_per_second * _worst_case_seconds(capability))

    def upper_bound(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        ceiling_rate = self.rate_per_second
        if self.bid_rate_per_second is not None:
            ceiling_rate = max(ceiling_rate, self.bid_rate_per_second)
        return _usd(ceiling_rate * _worst_case_seconds(capability))


class PerTokenPricing(_PricingBase):
    """Split prompt/completion token pricing with max-token upper bounds."""

    kind: Literal["per_token"] = "per_token"
    per_million_input_tokens: Decimal
    per_million_output_tokens: Decimal

    @field_validator("per_million_input_tokens", mode="before")
    @classmethod
    def _validate_input_rate(cls, value: object) -> Decimal:
        return _non_negative_decimal(value, "per_million_input_tokens")

    @field_validator("per_million_output_tokens", mode="before")
    @classmethod
    def _validate_output_rate(cls, value: object) -> Decimal:
        return _non_negative_decimal(value, "per_million_output_tokens")

    def estimate(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        in_tok, out_tok = _estimate_tokens(payload, capability)
        return self._tokens_to_usd(in_tok, out_tok)

    def upper_bound(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        in_tok = _estimate_input_token_count(payload)
        out_tok = _estimate_output_token_upper_bound(payload)
        return self._tokens_to_usd(in_tok, out_tok)

    def _tokens_to_usd(self, input_tokens: Decimal, output_tokens: Decimal) -> Decimal:
        return _usd(
            (
                self.per_million_input_tokens * input_tokens
                + self.per_million_output_tokens * output_tokens
            )
            / Decimal(1_000_000)
        )


class PerVmSecondPricing(_PricingBase):
    """Flat VM-second pricing for VM-style providers."""

    kind: Literal["per_vm_second"] = "per_vm_second"
    rate_per_second: Decimal

    @field_validator("rate_per_second", mode="before")
    @classmethod
    def _validate_rate_per_second(cls, value: object) -> Decimal:
        return _non_negative_decimal(value, "rate_per_second")

    def estimate(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        return _usd(self.rate_per_second * _worst_case_seconds(capability))

    def upper_bound(self, capability: Capability, payload: EstimatePayload) -> Decimal:
        return self.estimate(capability, payload)


type TaggedPricingModel = (
    GpuHourPricing | PerRequestPricing | PerSecondPricing | PerTokenPricing | PerVmSecondPricing
)
type PricingModel = Annotated[TaggedPricingModel, Field(discriminator="kind")]

_PRICING_MODEL_ADAPTER: TypeAdapter[TaggedPricingModel] = TypeAdapter(PricingModel)
_PRICING_MODEL_CLASSES = (
    GpuHourPricing,
    PerRequestPricing,
    PerSecondPricing,
    PerTokenPricing,
    PerVmSecondPricing,
)


@dataclass(frozen=True)
class CostQuote:
    """A provider pricing model bound to one capability and request payload."""

    pricing: TaggedPricingModel
    capability: Capability
    payload: EstimatePayload

    def estimate(self) -> Decimal:
        return self.pricing.estimate(self.capability, self.payload)

    def upper_bound(self) -> Decimal:
        return self.pricing.upper_bound(self.capability, self.payload)


@runtime_checkable
class CostEstimator(Protocol):
    """Protocol that every compatibility estimator must satisfy."""

    def estimate(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal: ...

    def upper_bound(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal: ...


def parse_pricing_model(
    provider_cost: object,
    *,
    cost_mode: CostMode | str | None = None,
) -> TaggedPricingModel:
    """Return a tagged pricing model from tagged or legacy provider cost data."""

    if isinstance(provider_cost, _PRICING_MODEL_CLASSES):
        return provider_cost

    cost = _cost_mapping(provider_cost)
    if "kind" in cost or "model" in cost:
        return _tagged_pricing_model(cost)

    mode = _required_cost_mode(cost_mode)
    if mode == CostMode.PER_SECOND:
        return GpuHourPricing(
            per_second_active=_required_non_negative_decimal(
                cost,
                "per_second_active",
            )
        )
    if mode == CostMode.PER_REQUEST:
        return PerRequestPricing(
            per_request=_required_non_negative_decimal(
                cost,
                "per_request",
            )
        )
    if mode == CostMode.PER_TOKEN:
        return PerTokenPricing(
            per_million_input_tokens=_required_non_negative_decimal(
                cost,
                "per_million_input_tokens",
            ),
            per_million_output_tokens=_required_non_negative_decimal(
                cost,
                "per_million_output_tokens",
            ),
        )
    raise ValueError(f"unsupported cost_mode: {cost_mode!r}")


def quote_cost(
    *,
    capability: Capability,
    provider_cost: object,
    payload: EstimatePayload,
) -> CostQuote:
    """Bind a tagged pricing model to one capability/payload for admission."""

    return CostQuote(
        pricing=parse_pricing_model(provider_cost, cost_mode=capability.cost_mode),
        capability=capability,
        payload=payload,
    )


class PerSecondEstimator:
    """Estimate cost for Pods and queue-based Serverless billed by container-second.

    Uses the capability's ``execution_timeout_ms`` as the worst-case runtime
    and multiplies by the provider's ``per_second_active`` rate.
    """

    def estimate(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal:
        return self._pricing(provider_cost).estimate(capability, payload)

    def upper_bound(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal:
        return self._pricing(provider_cost).upper_bound(capability, payload)

    @staticmethod
    def _pricing(provider_cost: object) -> TaggedPricingModel:
        return parse_pricing_model(provider_cost, cost_mode=CostMode.PER_SECOND)


class PerRequestEstimator:
    """Estimate cost for Public Endpoints with flat per-invocation pricing."""

    def estimate(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal:
        return self._pricing(provider_cost).estimate(capability, payload)

    def upper_bound(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal:
        return self._pricing(provider_cost).upper_bound(capability, payload)

    @staticmethod
    def _pricing(provider_cost: object) -> TaggedPricingModel:
        return parse_pricing_model(provider_cost, cost_mode=CostMode.PER_REQUEST)


class PerTokenEstimator:
    """Estimate cost for OpenAI-compatible endpoints.

    Reads ``per_million_input_tokens`` and ``per_million_output_tokens``
    from the provider cost dict and estimates token usage from the payload.
    """

    def estimate(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal:
        return self._pricing(provider_cost).estimate(capability, payload)

    def upper_bound(
        self,
        capability: Capability,
        provider_cost: ProviderCost,
        payload: EstimatePayload,
    ) -> Decimal:
        return self._pricing(provider_cost).upper_bound(capability, payload)

    @staticmethod
    def _pricing(provider_cost: object) -> TaggedPricingModel:
        return parse_pricing_model(provider_cost, cost_mode=CostMode.PER_TOKEN)

    @staticmethod
    def _estimate_tokens(
        payload: EstimatePayload,
        capability: Capability,
    ) -> tuple[Decimal, Decimal]:
        """Return (input_tokens, output_tokens) estimate.

        If the payload includes explicit token counts, use them.
        Otherwise fall back to heuristic estimates based on payload size
        and capability defaults.
        """
        return _estimate_tokens(payload, capability)


_REGISTRY: dict[CostMode, CostEstimator] = {
    CostMode.PER_SECOND: PerSecondEstimator(),
    CostMode.PER_REQUEST: PerRequestEstimator(),
    CostMode.PER_TOKEN: PerTokenEstimator(),
}


def get_estimator(mode: CostMode | str) -> CostEstimator:
    """Return the :class:`CostEstimator` for *mode*.

    Raises :class:`ValueError` for unknown modes.
    """
    normalized_mode = _required_cost_mode(mode)
    estimator = _REGISTRY.get(normalized_mode)
    if estimator is None:
        raise ValueError(f"unsupported cost_mode: {mode!r}")
    return estimator


def _tagged_pricing_model(cost: Mapping[str, Any]) -> TaggedPricingModel:
    data = dict(cost)
    model = data.pop("model", None)
    if "kind" not in data and model is not None:
        data["kind"] = model
    pricing_model: TaggedPricingModel = _PRICING_MODEL_ADAPTER.validate_python(data)
    return pricing_model


def _required_cost_mode(cost_mode: CostMode | str | None) -> CostMode:
    if cost_mode is None:
        raise ValueError("legacy provider cost requires cost_mode or tagged pricing kind")
    try:
        return CostMode(cost_mode)
    except ValueError as exc:
        raise ValueError(f"unsupported cost_mode: {cost_mode!r}") from exc


def _worst_case_seconds(capability: Capability) -> Decimal:
    return Decimal(capability.defaults.execution_timeout_ms) / Decimal(1_000)


def _cost_mapping(provider_cost: object) -> Mapping[str, Any]:
    """Return the provider's cost map from flat, nested, or model-like inputs."""

    if isinstance(provider_cost, Mapping):
        cost = provider_cost.get("cost")
        if isinstance(cost, Mapping):
            return cost
        config = provider_cost.get("config")
        if isinstance(config, Mapping):
            config_cost = config.get("cost")
            if isinstance(config_cost, Mapping):
                return config_cost
        return provider_cost

    cost = getattr(provider_cost, "cost", None)
    if isinstance(cost, Mapping):
        return cost

    config = getattr(provider_cost, "config", None)
    if isinstance(config, Mapping):
        config_cost = config.get("cost")
        if isinstance(config_cost, Mapping):
            return config_cost

    raise ValueError("provider cost must be a mapping or expose a mapping 'cost'")


def _required_non_negative_decimal(provider_cost: object, key: str) -> Decimal:
    cost = _cost_mapping(provider_cost)
    if key not in cost:
        raise ValueError(f"provider cost missing required key {key!r}")

    return _non_negative_decimal(cost[key], f"provider cost {key!r}")


def _non_negative_decimal(raw_value: object, name: str) -> Decimal:
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be a decimal value")

    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal value") from exc

    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _optional_non_negative_decimal(raw_value: object, name: str) -> Decimal | None:
    if raw_value is None:
        return None
    return _non_negative_decimal(raw_value, name)


_MISSING = object()


def _first_present(payload: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return _MISSING


def _token_count(payload: EstimatePayload, *keys: str) -> object:
    token_count = _first_present(payload, *keys)
    if token_count is not _MISSING:
        return token_count

    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        return _first_present(usage, *keys)
    return _MISSING


def _estimate_tokens(
    payload: EstimatePayload,
    capability: Capability,
) -> tuple[Decimal, Decimal]:
    return _estimate_input_token_count(payload), _estimate_output_token_count(payload)


def _estimate_input_token_count(payload: EstimatePayload) -> Decimal:
    input_tokens = _token_count(payload, "input_tokens", "prompt_tokens")
    if input_tokens is _MISSING:
        return _estimate_input_tokens(payload)
    return _non_negative_decimal(input_tokens, "input_tokens")


def _estimate_output_token_count(payload: EstimatePayload) -> Decimal:
    output_tokens = _token_count(payload, "output_tokens", "completion_tokens")
    if output_tokens is not _MISSING:
        return _non_negative_decimal(output_tokens, "output_tokens")

    max_output_tokens = _first_present(
        payload,
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "max_new_tokens",
    )
    if max_output_tokens is _MISSING:
        return Decimal(256)
    return _non_negative_decimal(max_output_tokens, "max_output_tokens")


def _estimate_output_token_upper_bound(payload: EstimatePayload) -> Decimal:
    max_output_tokens = _first_present(
        payload,
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "max_new_tokens",
    )
    if max_output_tokens is _MISSING:
        raise ValueError("max_output_tokens is required for per-token upper_bound")
    return _non_negative_decimal(max_output_tokens, "max_output_tokens")


def _estimate_input_tokens(payload: EstimatePayload) -> Decimal:
    input_bytes = _first_present(payload, "input_bytes")
    if input_bytes is not _MISSING:
        return _non_negative_decimal(input_bytes, "input_bytes") / Decimal(4)

    total_chars = sum(
        _text_length(payload[key])
        for key in ("system", "messages", "prompt", "input")
        if key in payload and payload[key] is not None
    )
    return Decimal(total_chars) / Decimal(4)


def _text_length(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return sum(
            _text_length(value[key])
            for key in ("content", "text", "prompt", "input")
            if key in value and value[key] is not None
        )
    if isinstance(value, list):
        return sum(_text_length(item) for item in value)
    return 0


def _usd(value: Decimal) -> Decimal:
    try:
        return value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        # Honor the estimator's ValueError-only contract: a cost too large to
        # quantize to the USD quantum (e.g. an absurd per_second_active rate) is
        # an invalid input, not an unhandled decimal error.
        raise ValueError(f"cost estimate is out of representable USD range: {value}") from exc


__all__ = [
    "CostQuote",
    "CostEstimator",
    "EstimatePayload",
    "GpuHourPricing",
    "PerRequestPricing",
    "PerRequestEstimator",
    "PerSecondPricing",
    "PerSecondEstimator",
    "PerTokenPricing",
    "PerTokenEstimator",
    "PerVmSecondPricing",
    "PricingModel",
    "PricingModelProtocol",
    "ProviderCost",
    "TaggedPricingModel",
    "get_estimator",
    "parse_pricing_model",
    "quote_cost",
]
