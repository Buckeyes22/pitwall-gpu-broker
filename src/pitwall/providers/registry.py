"""Provider plugin registry and credential validation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ValidationError

from pitwall.providers.interface import Provider


class ProviderRegistryError(RuntimeError):
    """Base class for provider registry errors."""


class DuplicateProviderError(ProviderRegistryError):
    """Raised when a provider id is registered more than once."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"provider {provider_id!r} is already registered")
        self.provider_id = provider_id


class ProviderNotRegisteredError(ProviderRegistryError):
    """Raised when a provider id is not present in the registry."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"provider {provider_id!r} is not registered")
        self.provider_id = provider_id


class CredentialValidationError(ProviderRegistryError):
    """Safe-to-log credential validation failure."""

    def __init__(
        self,
        provider_id: str,
        fields: Iterable[str],
    ) -> None:
        normalized_fields = tuple(dict.fromkeys(fields))
        field_list = ", ".join(normalized_fields) if normalized_fields else "<model>"
        super().__init__(
            f"credentials for provider {provider_id!r} failed validation for fields: {field_list}"
        )
        self.provider_id = provider_id
        self.fields = normalized_fields


class ProviderRegistry:
    """In-memory registry of provider plugins keyed by stable provider id."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    @property
    def ids(self) -> tuple[str, ...]:
        """Registered provider ids in registration order."""

        return tuple(self._providers)

    def register(self, provider: Provider, *, replace: bool = False) -> Provider:
        """Register *provider* and return it."""

        provider_id = _validated_provider_id(provider.id)
        _validated_credential_schema(provider.credential_schema)
        if provider_id in self._providers and not replace:
            raise DuplicateProviderError(provider_id)
        self._providers[provider_id] = provider
        return provider

    def lookup(self, provider_id: str) -> Provider:
        """Return the provider registered as *provider_id*."""

        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise ProviderNotRegisteredError(provider_id) from exc

    def validate_credentials(self, provider_id: str, credentials: object) -> BaseModel:
        """Validate *credentials* against the provider's declared schema."""

        provider = self.lookup(provider_id)
        schema = provider.credential_schema
        try:
            return schema.model_validate(credentials)
        except ValidationError as exc:
            raise CredentialValidationError(
                provider_id,
                _validation_error_fields(exc),
            ) from exc

    def credential_json_schema(self, provider_id: str) -> dict[str, Any]:
        """Return the provider credential JSON schema."""

        return dict(self.lookup(provider_id).credential_schema.model_json_schema())


def create_default_registry() -> ProviderRegistry:
    """Return a new registry with built-in provider plugins registered."""

    from pitwall.providers.lambda_cloud import LambdaCloudProvider
    from pitwall.providers.runpod import RunPodProvider
    from pitwall.providers.together import TogetherProvider
    from pitwall.providers.vast import VastProvider

    registry = ProviderRegistry()
    registry.register(RunPodProvider())
    registry.register(VastProvider())
    registry.register(TogetherProvider())
    registry.register(LambdaCloudProvider())
    return registry


_DEFAULT_REGISTRY: ProviderRegistry | None = None


def get_default_registry() -> ProviderRegistry:
    """Return the process-wide provider registry."""

    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = create_default_registry()
    return _DEFAULT_REGISTRY


def _validated_provider_id(provider_id: str) -> str:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider id must be a non-empty string")
    if provider_id != provider_id.strip():
        raise ValueError("provider id must not include surrounding whitespace")
    return provider_id


def _validated_credential_schema(schema: type[BaseModel]) -> type[BaseModel]:
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise ValueError("provider credential_schema must be a Pydantic BaseModel type")
    return schema


def _validation_error_fields(exc: ValidationError) -> tuple[str, ...]:
    fields: list[str] = []
    for item in exc.errors(include_input=False, include_url=False):
        loc = item.get("loc", ())
        if isinstance(loc, tuple) and loc:
            fields.append(".".join(str(part) for part in loc))
        elif isinstance(loc, str) and loc:
            fields.append(loc)
    return tuple(fields)


__all__ = [
    "CredentialValidationError",
    "DuplicateProviderError",
    "ProviderNotRegisteredError",
    "ProviderRegistry",
    "ProviderRegistryError",
    "create_default_registry",
    "get_default_registry",
]
