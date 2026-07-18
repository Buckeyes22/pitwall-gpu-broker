"""Together AI provider plugin adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, field_validator

from pitwall.core.enums import CostMode
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerTokenPricing, TaggedPricingModel, parse_pricing_model
from pitwall.providers.interface import (
    ProvisionRequest,
    ProvisionResult,
    ReconcileRequest,
    ReconcileResult,
    StatusRequest,
    StatusResult,
    TeardownRequest,
    TeardownResult,
)

SafeTogetherUrl = Annotated[str, AfterValidator(lambda value: _safe_together_url(value))]

_DEFAULT_BASE_URL = "https://api.together.xyz/v1"
_CHAT_COMPLETIONS_PATH = "chat/completions"
_MAX_ERROR_BODY_CHARS = 500


class TogetherCredentials(BaseModel):
    """Credentials required for Together API operations."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=1)
    base_url: SafeTogetherUrl = _DEFAULT_BASE_URL
    timeout_s: float = Field(default=330.0, gt=0)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must be non-empty")
        return value


class TogetherProviderError(RuntimeError):
    """Safe-to-log Together API failure."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        super().__init__(
            f"Together API request failed with HTTP {status_code}: {_compact_error_body(body)}"
        )


@dataclass(frozen=True, slots=True)
class TogetherInferenceResult:
    """OpenAI-compatible Together chat completion result."""

    content: str | None
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None
    raw: Mapping[str, Any] = field(default_factory=dict)


class TogetherProvider:
    """Provider plugin backed by Together's OpenAI-compatible inference API."""

    id = "together"
    name = "Together AI"
    credential_schema = TogetherCredentials

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def pricing_model(
        self,
        capability: Capability,
        provider_record: ProviderRecord,
    ) -> TaggedPricingModel:
        if CostMode(capability.cost_mode) != CostMode.PER_TOKEN:
            raise ValueError("TogetherProvider requires per_token capability cost_mode")
        pricing = parse_pricing_model(provider_record, cost_mode=CostMode.PER_TOKEN)
        if not isinstance(pricing, PerTokenPricing):
            raise ValueError("TogetherProvider requires per_token pricing")
        return pricing

    async def infer(
        self,
        *,
        credentials: TogetherCredentials,
        provider_record: ProviderRecord,
        payload: Mapping[str, Any],
    ) -> TogetherInferenceResult:
        """Run one Together chat completion with header-only authentication."""

        body = _payload_with_model(provider_record, payload)
        headers = {
            "Authorization": f"Bearer {credentials.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=credentials.base_url,
            timeout=credentials.timeout_s,
            transport=self._transport,
        ) as client:
            response = await client.post(_CHAT_COMPLETIONS_PATH, headers=headers, json=body)

        if response.status_code >= 400:
            raise TogetherProviderError(response.status_code, response.text)

        data = _json_response(response)
        return _inference_result(data, fallback_model=str(body["model"]))

    async def provision(self, request: ProvisionRequest) -> ProvisionResult:
        raise NotImplementedError("TogetherProvider is inference-only; use infer()")

    async def status(self, request: StatusRequest) -> StatusResult:
        raise NotImplementedError("TogetherProvider is inference-only; use infer()")

    async def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        raise NotImplementedError("TogetherProvider is inference-only; use infer()")

    async def teardown(self, request: TeardownRequest) -> TeardownResult:
        raise NotImplementedError("TogetherProvider is inference-only; use infer()")


def _payload_with_model(
    provider_record: ProviderRecord,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    body = dict(payload)
    priced_model = _provider_model(provider_record)
    if priced_model is None:
        raise ValueError("Together inference requires a non-empty provider-priced model")
    requested_model = _optional_non_empty_string(body.get("model"))
    if requested_model is not None and requested_model != priced_model:
        raise ValueError(
            f"Together payload model must match the provider-priced model {priced_model!r}"
        )
    body["model"] = priced_model
    return body


def _provider_model(provider_record: ProviderRecord) -> str | None:
    config = provider_record.config
    if not isinstance(config, Mapping):
        return None
    for key in ("model", "model_id", "together_model"):
        model = _optional_non_empty_string(config.get(key))
        if model is not None:
            return model
    return None


def _json_response(response: httpx.Response) -> Mapping[str, Any]:
    try:
        raw: object = json.loads(response.text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise TogetherProviderError(response.status_code, "response body was not JSON") from exc
    if not isinstance(raw, Mapping):
        raise TogetherProviderError(response.status_code, "response body was not a JSON object")
    return raw


def _inference_result(
    data: Mapping[str, Any],
    *,
    fallback_model: str,
) -> TogetherInferenceResult:
    choice = _first_choice(data.get("choices"))
    message = choice.get("message")
    message_body = message if isinstance(message, Mapping) else {}
    usage = data.get("usage")
    usage_body = usage if isinstance(usage, Mapping) else {}
    prompt_tokens = _optional_token_count(usage_body.get("prompt_tokens"))
    completion_tokens = _optional_token_count(usage_body.get("completion_tokens"))
    total_tokens = _optional_token_count(usage_body.get("total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    return TogetherInferenceResult(
        content=_optional_non_empty_string(message_body.get("content"))
        or _optional_non_empty_string(choice.get("text")),
        model=_optional_non_empty_string(data.get("model")) or fallback_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        finish_reason=_optional_non_empty_string(choice.get("finish_reason")),
        raw=dict(data),
    )


def _first_choice(choices: object) -> Mapping[str, Any]:
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, Mapping):
            return choice
    return {}


def _optional_token_count(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, Decimal):
        integral = value.to_integral_value()
        return int(integral) if integral == value and integral >= 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal():
            return int(stripped)
    return None


def _optional_non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _safe_together_url(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("url must be non-empty")
    parsed = urlsplit(stripped)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not include user info")
    if parsed.query or parsed.fragment:
        raise ValueError("url must not include query strings or fragments")
    return stripped.rstrip("/")


def _compact_error_body(body: str) -> str:
    normalized = " ".join(body.split())
    if not normalized:
        return "<empty body>"
    if len(normalized) <= _MAX_ERROR_BODY_CHARS:
        return normalized
    return f"{normalized[:_MAX_ERROR_BODY_CHARS]}..."


__all__ = [
    "SafeTogetherUrl",
    "TogetherCredentials",
    "TogetherInferenceResult",
    "TogetherProvider",
    "TogetherProviderError",
]
