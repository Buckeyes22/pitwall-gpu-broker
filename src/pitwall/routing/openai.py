"""OpenAI-compatible provider chain resolution.

The OpenAI pass-through route needs a deterministic list of upstream providers
to try inside one request.  This module keeps that decision pure: it orders
provider records, removes unusable providers, and caps the chain at three total
attempts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from posixpath import normpath
from typing import Any, cast
from urllib.parse import unquote, urlsplit, urlunsplit

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.routing.cooldown import is_in_cooldown

DEFAULT_OPENAI_MAX_ATTEMPTS = 3
MAX_OPENAI_ATTEMPTS = DEFAULT_OPENAI_MAX_ATTEMPTS
OPENAI_PROVIDER_TYPES = frozenset(
    {
        ProviderType.SERVERLESS_QUEUE.value,
        ProviderType.SERVERLESS_LB.value,
        ProviderType.PUBLIC_ENDPOINT.value,
    }
)
_OPENAI_PROXY_PATH_ERROR = "OpenAI proxy path must be a normalized relative path"

_ProviderLike = Provider | Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OpenAIChainAttempt:
    """One provider attempt in an OpenAI-compatible fallback chain."""

    provider_id: str
    provider: _ProviderLike
    attempt: int
    openai_base_url: str | None = None
    backoff_before_attempt_s: float = 0.0

    @property
    def attempt_number(self) -> int:
        return self.attempt

    @property
    def base_url(self) -> str | None:
        return self.openai_base_url

    def to_dict(self) -> dict[str, int | float | str | None]:
        return {
            "attempt": self.attempt,
            "provider_id": self.provider_id,
            "openai_base_url": self.openai_base_url,
            "base_url": self.openai_base_url,
            "backoff_before_attempt_s": self.backoff_before_attempt_s,
        }


@dataclass(frozen=True, slots=True)
class OpenAIProviderChain:
    """Deterministic provider chain for an OpenAI-compatible request."""

    attempts: tuple[OpenAIChainAttempt, ...] = field(default_factory=tuple)
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS

    def __iter__(self) -> Iterator[_ProviderLike]:
        return iter(self.providers)

    def __len__(self) -> int:
        return len(self.attempts)

    def __getitem__(self, index: int | slice) -> _ProviderLike | tuple[_ProviderLike, ...]:
        return self.providers[index]

    @property
    def selected_provider_id(self) -> str | None:
        if not self.attempts:
            return None
        return self.attempts[0].provider_id

    @property
    def selected_provider(self) -> _ProviderLike | None:
        if not self.attempts:
            return None
        return self.attempts[0].provider

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(attempt.provider_id for attempt in self.attempts)

    @property
    def fallback_chain(self) -> tuple[str, ...]:
        return self.provider_ids

    @property
    def provider_chain(self) -> tuple[str, ...]:
        return self.provider_ids

    @property
    def fallback_provider_ids(self) -> tuple[str, ...]:
        return self.provider_ids[1:]

    @property
    def providers(self) -> tuple[_ProviderLike, ...]:
        return tuple(attempt.provider for attempt in self.attempts)

    @property
    def fallback_providers(self) -> tuple[_ProviderLike, ...]:
        return self.providers[1:]

    @property
    def base_urls(self) -> tuple[str | None, ...]:
        return tuple(attempt.openai_base_url for attempt in self.attempts)

    @property
    def openai_base_urls(self) -> tuple[str | None, ...]:
        return self.base_urls

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_provider_id": self.selected_provider_id,
            "provider_ids": list(self.provider_ids),
            "fallback_chain": list(self.fallback_chain),
            "fallback_provider_ids": list(self.fallback_provider_ids),
            "openai_base_urls": list(self.openai_base_urls),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "max_attempts": self.max_attempts,
        }


def resolve_openai_provider_chain(
    providers: Iterable[_ProviderLike],
    *,
    primary_provider_id: str | None = None,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
    now: dt.datetime | None = None,
) -> OpenAIProviderChain:
    """Resolve the ordered OpenAI-compatible provider chain.

    Providers are ordered by ``priority`` ascending, then ``name``, then ``id``.
    If the primary provider declares ``fallback_chain`` in its config, that
    explicit order is used immediately after the primary and the rest of the
    deterministically ordered providers fill any remaining attempt slots.
    """

    effective_max_attempts = _effective_max_attempts(max_attempts)
    ordered = ordered_openai_providers(
        providers,
        primary_provider_id=primary_provider_id,
        max_attempts=effective_max_attempts,
        now=now,
    )
    return OpenAIProviderChain(
        attempts=tuple(
            OpenAIChainAttempt(
                provider_id=_provider_id(provider),
                provider=provider,
                attempt=index,
                openai_base_url=openai_base_url_for_provider(provider),
                backoff_before_attempt_s=_backoff_before_attempt(index),
            )
            for index, provider in enumerate(ordered, start=1)
        ),
        max_attempts=effective_max_attempts,
    )


def resolve_openai_chain(
    providers: Iterable[_ProviderLike],
    *,
    primary_provider_id: str | None = None,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
    now: dt.datetime | None = None,
) -> OpenAIProviderChain:
    """Backward-friendly alias for ``resolve_openai_provider_chain``."""

    return resolve_openai_provider_chain(
        providers,
        primary_provider_id=primary_provider_id,
        max_attempts=max_attempts,
        now=now,
    )


def resolve_provider_chain(
    providers: Iterable[_ProviderLike],
    *,
    primary_provider_id: str | None = None,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
    now: dt.datetime | None = None,
) -> OpenAIProviderChain:
    """Alias used by callers that import this as the chain resolver module."""

    return resolve_openai_provider_chain(
        providers,
        primary_provider_id=primary_provider_id,
        max_attempts=max_attempts,
        now=now,
    )


def resolve_openai_provider_ids(
    providers: Iterable[_ProviderLike],
    *,
    primary_provider_id: str | None = None,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
    now: dt.datetime | None = None,
) -> tuple[str, ...]:
    """Return only provider ids for the resolved OpenAI-compatible chain."""

    return resolve_openai_provider_chain(
        providers,
        primary_provider_id=primary_provider_id,
        max_attempts=max_attempts,
        now=now,
    ).provider_ids


def ordered_openai_providers(
    providers: Iterable[_ProviderLike],
    *,
    primary_provider_id: str | None = None,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
    now: dt.datetime | None = None,
) -> tuple[_ProviderLike, ...]:
    """Return OpenAI-compatible providers in deterministic attempt order."""

    effective_max_attempts = _effective_max_attempts(max_attempts)
    candidates = tuple(
        provider
        for provider in providers
        if is_openai_compatible_provider(provider)
        and _provider_enabled(provider)
        and not _provider_unhealthy(provider)
        and not _provider_in_cooldown(provider, now=now)
    )
    ordered_candidates = tuple(sorted(candidates, key=_provider_order_key))
    if not ordered_candidates:
        return ()

    by_id = {_provider_id(provider): provider for provider in ordered_candidates}
    primary = _resolve_primary_provider(
        ordered_candidates,
        by_id=by_id,
        primary_provider_id=primary_provider_id,
    )

    chain: list[_ProviderLike] = []
    seen: set[str] = set()

    def append_provider(provider: _ProviderLike | None) -> None:
        if provider is None or len(chain) >= effective_max_attempts:
            return
        provider_id = _provider_id(provider)
        if provider_id in seen:
            return
        seen.add(provider_id)
        chain.append(provider)

    append_provider(primary)
    for fallback_id in _explicit_fallback_chain(primary):
        append_provider(by_id.get(fallback_id))

    for provider in ordered_candidates:
        append_provider(provider)

    return tuple(chain)


def is_openai_compatible_provider(provider: _ProviderLike) -> bool:
    """Return whether a provider can participate in the OpenAI pass-through."""

    provider_type = _provider_type(provider)
    if provider_type is None:
        return True
    return provider_type in OPENAI_PROVIDER_TYPES


def openai_base_url_for_provider(provider: _ProviderLike) -> str | None:
    """Return the configured or derived OpenAI-compatible base URL, if known."""

    configured = _non_empty_string(_config_value(provider, "openai_base_url"))
    if configured is not None:
        return configured.rstrip("/")

    endpoint_id = _non_empty_string(_field(provider, "runpod_endpoint_id"))
    if endpoint_id is None:
        endpoint_id = _non_empty_string(_config_value(provider, "runpod_endpoint_id"))
    if endpoint_id is None:
        endpoint_id = _non_empty_string(_config_value(provider, "endpoint_id"))
    if endpoint_id is None:
        return None

    provider_type = _provider_type(provider)
    if provider_type == ProviderType.SERVERLESS_LB.value:
        return f"https://{endpoint_id}.api.runpod.ai/openai/v1"
    if provider_type in (
        ProviderType.SERVERLESS_QUEUE.value,
        ProviderType.PUBLIC_ENDPOINT.value,
        None,
    ):
        return f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1"
    return None


def validate_openai_proxy_path(path: str) -> None:
    """Raise ValueError if ``path`` is not a safe relative OpenAI proxy path.

    Runs the exact validation :func:`build_openai_url` applies, so callers can
    fail fast (e.g. HTTP 400) before any provider resolution or budget work.
    """
    build_openai_url("https://validation.invalid/v1", path)


def build_openai_url(openai_base_url: str, path: str) -> str:
    """Build a full OpenAI-compatible URL by combining base URL and path.

    This function handles the case where the path might include a leading ``v1/``
    segment that would duplicate the ``/v1`` already present at the end of
    ``openai_base_url``.

    Args:
        openai_base_url: The OpenAI-compatible base URL
            (e.g. ``https://api.runpod.ai/v2/<endpoint_id>/openai/v1``).
        path: The request path, with or without a leading slash and with or
            without a leading ``v1/`` segment
            (e.g. ``/chat/completions?x=1`` or ``v1/chat/completions``).

    Returns:
        The fully-combined URL without any duplicated ``/v1`` segment.

    Raises:
        ValueError: If ``path`` is not a normalized relative OpenAI path.

    Examples:
        >>> build_openai_url(
        ...     "https://api.runpod.ai/v2/ep/openai/v1",
        ...     "/chat/completions?x=1"
        ... )
        'https://api.runpod.ai/v2/ep/openai/v1/chat/completions?x=1'

        >>> build_openai_url(
        ...     "https://api.runpod.ai/v2/ep/openai/v1",
        ...     "v1/chat/completions"
        ... )
        'https://api.runpod.ai/v2/ep/openai/v1/chat/completions'
    """
    _raise_for_unsafe_openai_path(path, allow_leading_slash=True)

    path = path.lstrip("/")
    if path.startswith("v1/"):
        path = path[3:]

    _raise_for_unsafe_openai_path(path, allow_leading_slash=False)
    parsed_path = urlsplit(path)
    decoded_path = unquote(parsed_path.path)
    if decoded_path.startswith("//") or any(
        segment in {".", ".."} for segment in decoded_path.split("/")
    ):
        raise ValueError(_OPENAI_PROXY_PATH_ERROR)

    normalized_path = normpath(parsed_path.path)
    if normalized_path == ".":
        normalized_path = ""
    if (
        normalized_path.startswith("/")
        or normalized_path == ".."
        or normalized_path.startswith("../")
    ):
        raise ValueError(_OPENAI_PROXY_PATH_ERROR)

    base = urlsplit(openai_base_url.rstrip("/") + "/")
    base_path = base.path.rstrip("/")
    target_path = f"{base_path}/{normalized_path}" if normalized_path else f"{base_path}/"
    return urlunsplit(
        (
            base.scheme,
            base.netloc,
            target_path,
            parsed_path.query,
            parsed_path.fragment,
        )
    )


def _raise_for_unsafe_openai_path(path: str, *, allow_leading_slash: bool) -> None:
    parsed = urlsplit(path)
    if "\\" in path or path.startswith("//") or parsed.scheme or parsed.netloc:
        raise ValueError(_OPENAI_PROXY_PATH_ERROR)
    if not allow_leading_slash and path.startswith("/"):
        raise ValueError(_OPENAI_PROXY_PATH_ERROR)


def _resolve_primary_provider(
    ordered_candidates: tuple[_ProviderLike, ...],
    *,
    by_id: Mapping[str, _ProviderLike],
    primary_provider_id: str | None,
) -> _ProviderLike:
    if primary_provider_id is None:
        return ordered_candidates[0]
    primary = by_id.get(primary_provider_id)
    if primary is None:
        raise ValueError(f"primary_provider_id {primary_provider_id!r} is not available")
    return primary


def _effective_max_attempts(max_attempts: int) -> int:
    if isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    return min(max_attempts, DEFAULT_OPENAI_MAX_ATTEMPTS)


def _provider_order_key(provider: _ProviderLike) -> tuple[int, str, str]:
    return (_provider_priority(provider), _provider_name(provider), _provider_id(provider))


def _provider_id(provider: _ProviderLike) -> str:
    provider_id = _non_empty_string(_field(provider, "id"))
    if provider_id is None:
        raise ValueError("provider must include a non-empty id")
    return provider_id


def _provider_name(provider: _ProviderLike) -> str:
    return _non_empty_string(_field(provider, "name")) or ""


def _provider_priority(provider: _ProviderLike) -> int:
    value = _field(provider, "priority")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("provider priority must be an integer")
    return int(cast(int | float | str | bool, value))


def _provider_type(provider: _ProviderLike) -> str | None:
    return _non_empty_string(_field(provider, "provider_type"))


def _provider_enabled(provider: _ProviderLike) -> bool:
    return _field(provider, "enabled") is not False


def _provider_unhealthy(provider: _ProviderLike) -> bool:
    health_status = _non_empty_string(_field(provider, "health_status"))
    return health_status is not None and health_status.lower() == "unhealthy"


def _provider_in_cooldown(
    provider: _ProviderLike,
    *,
    now: dt.datetime | None,
) -> bool:
    if now is None:
        return False
    return is_in_cooldown(provider, now=now)


def _explicit_fallback_chain(provider: _ProviderLike) -> tuple[str, ...]:
    return _string_tuple(
        _first_present(
            _field(provider, "fallback_chain"),
            _config_value(provider, "fallback_chain"),
            _config_value(provider, "fallback_provider_ids"),
            _config_value(provider, "fallbacks"),
        )
    )


def _backoff_before_attempt(attempt: int) -> float:
    if attempt <= 1:
        return 0.0
    return float(2 ** (attempt - 2))


def _field(provider: _ProviderLike, key: str) -> object:
    if isinstance(provider, Mapping):
        return provider.get(key)
    return getattr(provider, key, None)


def _config(provider: _ProviderLike) -> Mapping[str, Any]:
    raw = _field(provider, "config")
    if isinstance(raw, Mapping):
        return raw
    return {}


def _config_value(provider: _ProviderLike, key: str) -> object:
    config = _config(provider)
    if key in config:
        return config[key]
    constraints = config.get("constraints")
    if isinstance(constraints, Mapping):
        return constraints.get(key)
    return None


def _first_present(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _non_empty_string(value: object) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ValueError("fallback provider ids must be strings or sequences of strings")

    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("fallback provider ids must be non-empty strings")
        if item not in result:
            result.append(item)
    return tuple(result)


__all__ = [
    "DEFAULT_OPENAI_MAX_ATTEMPTS",
    "MAX_OPENAI_ATTEMPTS",
    "OPENAI_PROVIDER_TYPES",
    "OpenAIChainAttempt",
    "OpenAIProviderChain",
    "build_openai_url",
    "is_openai_compatible_provider",
    "openai_base_url_for_provider",
    "ordered_openai_providers",
    "resolve_openai_chain",
    "resolve_openai_provider_chain",
    "resolve_openai_provider_ids",
    "resolve_provider_chain",
]
