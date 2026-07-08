"""Seed-file loading for Pitwall onboarding.

The loader accepts a deliberately small YAML/JSON shape so new operators can
bootstrap a capability and provider without GPU discovery or optional services.
PyYAML is used when available; otherwise a local YAML subset parser handles the
example seed files and the documented onboarding format.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pitwall.api.provider_schemas import (
    expected_lb_base_url,
    expected_openai_base_url,
    validate_provider_registration_config,
)
from pitwall.core.enums import (
    CapabilityClass,
    CapabilityHint,
    CapabilitySource,
    CostMode,
    ProviderType,
)
from pitwall.core.models import Capability, CapabilityDefaults, JsonObject, Provider
from pitwall.db.repository import CapabilityRepository, ProviderRepository
from pitwall.runpod_client.gpu import validate_canonical_gpu_name

_SEED_FILE_SUFFIXES = {".yaml", ".yml", ".json"}
_PROVIDER_HEALTH_STATUSES = {"unknown", "healthy", "unhealthy", "hibernated"}


class SeedValidationError(ValueError):
    """Raised when a seed file is syntactically valid but not usable."""


@dataclass(frozen=True)
class SeedDocument:
    """Parsed seed file with the content hash used for audit metadata."""

    path: Path
    payload: Mapping[str, Any]
    content_hash: str


@dataclass(frozen=True)
class SeedApplyResult:
    """Records written by a seed application."""

    capabilities: list[Capability]
    providers: list[Provider]


@dataclass(frozen=True)
class _YamlLine:
    indent: int
    text: str


def seed_files_from_paths(paths: Sequence[str | Path]) -> list[Path]:
    """Return concrete seed files from file or directory arguments."""

    if not paths:
        raise SeedValidationError("at least one seed file or directory is required")

    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(
                sorted(
                    child
                    for child in path.iterdir()
                    if child.is_file() and child.suffix.lower() in _SEED_FILE_SUFFIXES
                )
            )
            continue
        if not path.exists():
            raise SeedValidationError(f"seed path does not exist: {path}")
        if not path.is_file():
            raise SeedValidationError(f"seed path is not a file: {path}")
        files.append(path)

    if not files:
        raise SeedValidationError("no seed files found")
    return files


def load_seed_documents(paths: Sequence[str | Path]) -> list[SeedDocument]:
    """Load and parse seed documents from files or directories."""

    documents: list[SeedDocument] = []
    for path in seed_files_from_paths(paths):
        text = path.read_text(encoding="utf-8")
        payload = _load_seed_payload(path, text)
        documents.append(
            SeedDocument(
                path=path,
                payload=payload,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return documents


async def apply_capability_seed_files(
    paths: Sequence[str | Path],
    *,
    pool: Any,
    source: CapabilitySource = CapabilitySource.API,
) -> list[Capability]:
    """Apply only capability entries from seed/spec files."""

    documents = load_seed_documents(paths)
    cap_repo = CapabilityRepository(pool)
    capabilities, _capability_by_ref = await _apply_capabilities(
        documents=documents,
        cap_repo=cap_repo,
        source=source,
    )
    return capabilities


async def apply_seed_files(
    paths: Sequence[str | Path],
    *,
    pool: Any,
    source: CapabilitySource = CapabilitySource.YAML,
) -> SeedApplyResult:
    """Apply capability and provider seed files to the registry tables."""

    documents = load_seed_documents(paths)
    return await _apply_seed_documents(documents, pool=pool, source=source)


async def apply_seed_data(
    payload: Mapping[str, Any],
    *,
    pool: Any,
    source: CapabilitySource = CapabilitySource.API,
    content_hash: str | None = None,
) -> SeedApplyResult:
    """Apply an in-memory seed payload, used by the manual init path."""

    document = SeedDocument(
        path=Path("<manual>"),
        payload=payload,
        content_hash=content_hash or _stable_payload_hash(payload),
    )
    return await _apply_seed_documents([document], pool=pool, source=source)


async def _apply_seed_documents(
    documents: Sequence[SeedDocument],
    *,
    pool: Any,
    source: CapabilitySource,
) -> SeedApplyResult:
    cap_repo = CapabilityRepository(pool)
    provider_repo = ProviderRepository(pool)
    capabilities, capability_by_ref = await _apply_capabilities(
        documents=documents,
        cap_repo=cap_repo,
        source=source,
    )

    providers: list[Provider] = []
    for spec, document in _iter_provider_specs(documents):
        provider = await _provider_from_seed(
            spec,
            document=document,
            cap_repo=cap_repo,
            capability_by_ref=capability_by_ref,
            source=source,
        )
        existing = await provider_repo.get_by_name(provider.name)
        if existing is not None and existing.id != provider.id:
            raise SeedValidationError(
                f"provider {provider.name!r} already exists as {existing.id}; "
                "choose a different name or id"
            )
        providers.append(await provider_repo.create(provider))

    return SeedApplyResult(capabilities=capabilities, providers=providers)


async def _apply_capabilities(
    *,
    documents: Sequence[SeedDocument],
    cap_repo: CapabilityRepository,
    source: CapabilitySource,
) -> tuple[list[Capability], dict[str, Capability]]:
    capabilities: list[Capability] = []
    capability_by_ref: dict[str, Capability] = {}

    for spec, document in _iter_capability_specs(documents):
        name = _required_string(spec, "name", "capability.name")
        existing = await cap_repo.get_by_name(name)
        capability = _capability_from_seed(
            spec,
            document=document,
            source=source,
            existing_id=existing.id if existing is not None else None,
        )
        saved = await cap_repo.create(capability)
        capabilities.append(saved)
        capability_by_ref[saved.id] = saved
        capability_by_ref[saved.name] = saved

    return capabilities, capability_by_ref


def _load_seed_payload(path: Path, text: str) -> Mapping[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}

    if stripped[0] in "[{":
        payload = json.loads(stripped)
    else:
        try:
            import yaml  # type: ignore[import-untyped]  # reason: PyYAML ships no type stubs
        except ModuleNotFoundError:
            payload = _parse_simple_yaml(stripped)
        else:
            payload = yaml.safe_load(stripped)

    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise SeedValidationError(f"{path}: seed root must be an object")
    return payload


def _iter_capability_specs(
    documents: Sequence[SeedDocument],
) -> Iterable[tuple[Mapping[str, Any], SeedDocument]]:
    for document in documents:
        payload = document.payload
        raw_items = payload.get("capabilities")
        if raw_items is None and _looks_like_capability(payload):
            raw_items = [payload]
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            raise SeedValidationError(f"{document.path}: capabilities must be a list")
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise SeedValidationError(f"{document.path}: capability entries must be objects")
            yield item, document


def _iter_provider_specs(
    documents: Sequence[SeedDocument],
) -> Iterable[tuple[Mapping[str, Any], SeedDocument]]:
    for document in documents:
        payload = document.payload
        raw_items = payload.get("providers")
        if raw_items is None and _looks_like_provider(payload):
            raw_items = [payload]
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            raise SeedValidationError(f"{document.path}: providers must be a list")
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise SeedValidationError(f"{document.path}: provider entries must be objects")
            yield item, document


def _looks_like_capability(payload: Mapping[str, Any]) -> bool:
    return "name" in payload and ("class" in payload or "capability_class" in payload)


def _looks_like_provider(payload: Mapping[str, Any]) -> bool:
    provider_keys = {"endpoint_id", "runpod_endpoint_id", "provider_type", "type"}
    return "name" in payload and bool(provider_keys.intersection(payload))


def _capability_from_seed(
    spec: Mapping[str, Any],
    *,
    document: SeedDocument,
    source: CapabilitySource,
    existing_id: str | None,
) -> Capability:
    now = dt.datetime.now(dt.UTC)
    name = _required_string(spec, "name", "capability.name")
    class_value = _string_choice(
        _first_present(spec, ("class", "class_", "capability_class"), default="custom"),
        CapabilityClass,
        "capability.class",
    )
    cost_mode = _string_choice(
        _first_present(spec, ("cost_mode", "costMode"), default="per_second"),
        CostMode,
        "capability.cost_mode",
    )
    hints = [
        _string_choice(hint, CapabilityHint, "capability.hints_supported")
        for hint in _list_value(spec.get("hints_supported", []), "capability.hints_supported")
    ]
    return Capability(
        id=_optional_string(spec.get("id")) or existing_id or _id_from_name("cap", name),
        name=name,
        version=_optional_string(spec.get("version")) or "1.0.0",
        class_=class_value,
        description=_optional_string(spec.get("description")),
        input_schema=_dict_value(spec.get("input_schema", {}), "capability.input_schema"),
        output_schema=_dict_value(spec.get("output_schema", {}), "capability.output_schema"),
        defaults=CapabilityDefaults.model_validate(
            _dict_value(spec.get("defaults", {}), "capability.defaults")
        ),
        cost_mode=cost_mode,
        hints_supported=hints,
        source=source,
        last_applied_yaml_hash=document.content_hash if source == CapabilitySource.YAML else None,
        enabled=True,
        created_at=now,
        updated_at=now,
    )


async def _provider_from_seed(
    spec: Mapping[str, Any],
    *,
    document: SeedDocument,
    cap_repo: CapabilityRepository,
    capability_by_ref: Mapping[str, Capability],
    source: CapabilitySource,
) -> Provider:
    now = dt.datetime.now(dt.UTC)
    name = _required_string(spec, "name", "provider.name")
    provider_type = _string_choice(
        _first_present(spec, ("provider_type", "type"), required=True, field_name="provider.type"),
        ProviderType,
        "provider.provider_type",
    )
    endpoint_id = _optional_string(_first_present(spec, ("endpoint_id", "runpod_endpoint_id")))
    capability = await _resolve_capability(
        spec, cap_repo=cap_repo, capability_by_ref=capability_by_ref
    )
    gpu_class = validate_canonical_gpu_name(
        _required_string(spec, "gpu_class", "provider.gpu_class")
    )
    cloud_type = _optional_string(spec.get("cloud_type"))
    config = _provider_config(spec, provider_type=provider_type, endpoint_id=endpoint_id)
    config["gpu_class"] = gpu_class
    validate_provider_registration_config(
        provider_type=provider_type,
        endpoint_id=endpoint_id,
        cloud_type=cloud_type,
        config=config,
    )

    health_status = _optional_string(_first_present(spec, ("health_status", "health"))) or "unknown"
    if health_status not in _PROVIDER_HEALTH_STATUSES:
        allowed = ", ".join(sorted(_PROVIDER_HEALTH_STATUSES))
        raise SeedValidationError(f"provider.health_status must be one of: {allowed}")

    return Provider(
        id=_optional_string(spec.get("id")) or _id_from_name("prov", name),
        capability_id=capability.id,
        name=name,
        provider_type=provider_type,
        runpod_endpoint_id=endpoint_id,
        runpod_template_id=_optional_string(spec.get("runpod_template_id")),
        region=_optional_string(spec.get("region")),
        cloud_type=cloud_type,
        config=config,
        priority=_int_value(spec.get("priority", 0), "provider.priority"),
        enabled=bool(spec.get("enabled", True)),
        health_status=health_status,
        consecutive_failures=0,
        cooldown_trips=0,
        cold_start_p50_ms=None,
        cold_start_p95_ms=None,
        recent_error_rate=0.0,
        cooldown_until=None,
        source=source,
        last_applied_yaml_hash=document.content_hash if source == CapabilitySource.YAML else None,
        updated_at=now,
    )


async def _resolve_capability(
    spec: Mapping[str, Any],
    *,
    cap_repo: CapabilityRepository,
    capability_by_ref: Mapping[str, Capability],
) -> Capability:
    ref = _optional_string(_first_present(spec, ("capability", "capability_name", "capability_id")))
    if ref is None:
        raise SeedValidationError(
            "provider must include capability, capability_name, or capability_id"
        )
    if ref in capability_by_ref:
        return capability_by_ref[ref]

    if ref.startswith("cap_"):
        capability = await cap_repo.get(ref)
    else:
        capability = await cap_repo.get_by_name(ref)
    if capability is None:
        raise SeedValidationError(f"provider references unknown capability: {ref}")
    return capability


def _provider_config(
    spec: Mapping[str, Any],
    *,
    provider_type: ProviderType,
    endpoint_id: str | None,
) -> JsonObject:
    config = dict(_dict_value(spec.get("config", {}), "provider.config"))
    raw_cost = _first_present(spec, ("cost",), default=config.get("cost", {}))
    cost = dict(_dict_value(raw_cost, "provider.cost"))
    if "mode" not in cost:
        cost["mode"] = "per_second"
    config["cost"] = cost
    config["workers"] = dict(
        _dict_value(
            _first_present(spec, ("workers",), default=config.get("workers", {"workers_min": 0})),
            "provider.workers",
        )
    )
    config["idle_timeout_minutes"] = _int_value(
        _first_present(
            spec,
            ("idle_timeout_minutes", "idleTimeoutMinutes"),
            default=config.get("idle_timeout_minutes", 0),
        ),
        "provider.idle_timeout_minutes",
    )
    config["flash_boot_verified"] = bool(
        _first_present(
            spec,
            ("flash_boot_verified", "flashBootVerified"),
            default=config.get("flash_boot_verified", False),
        )
    )
    config["max_payload_mb"] = _int_value(
        _first_present(spec, ("max_payload_mb",), default=config.get("max_payload_mb", 30)),
        "provider.max_payload_mb",
    )
    config["request_timeout_s"] = _int_value(
        _first_present(
            spec,
            ("request_timeout_s",),
            default=config.get("request_timeout_s", 330),
        ),
        "provider.request_timeout_s",
    )
    if endpoint_id is not None:
        if provider_type == ProviderType.SERVERLESS_LB:
            config.setdefault("lb_base_url", expected_lb_base_url(endpoint_id))
        elif provider_type in (ProviderType.SERVERLESS_QUEUE, ProviderType.PUBLIC_ENDPOINT):
            config.setdefault(
                "openai_base_url", expected_openai_base_url(provider_type, endpoint_id)
            )
    return config


def _first_present(
    spec: Mapping[str, Any],
    names: Sequence[str],
    *,
    default: Any = None,
    required: bool = False,
    field_name: str | None = None,
) -> Any:
    for name in names:
        if name in spec:
            return spec[name]
    if required:
        raise SeedValidationError(f"{field_name or names[0]} is required")
    return default


def _required_string(spec: Mapping[str, Any], key: str, field_name: str) -> str:
    value = _optional_string(spec.get(key))
    if value is None:
        raise SeedValidationError(f"{field_name} is required")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_choice(value: object, enum_type: type[Any], field_name: str) -> Any:
    text = _optional_string(value)
    if text is None:
        raise SeedValidationError(f"{field_name} is required")
    try:
        return enum_type(text)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise SeedValidationError(f"{field_name} must be one of: {allowed}") from exc


def _dict_value(value: object, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SeedValidationError(f"{field_name} must be an object")
    return dict(value)


def _list_value(value: object, field_name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SeedValidationError(f"{field_name} must be a list")
    return list(value)


def _int_value(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise SeedValidationError(f"{field_name} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise SeedValidationError(f"{field_name} must be an integer") from exc


def _id_from_name(prefix: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not slug:
        raise SeedValidationError(f"{prefix} id cannot be generated from an empty name")
    return f"{prefix}_{slug}"


def _stable_payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_simple_yaml(text: str) -> Any:
    lines = _yaml_lines(text)
    if not lines:
        return {}
    value, index = _parse_yaml_block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise SeedValidationError(f"could not parse YAML near: {lines[index].text}")
    return value


def _yaml_lines(text: str) -> list[_YamlLine]:
    lines: list[_YamlLine] = []
    for raw_line in text.splitlines():
        line = _strip_yaml_comment(raw_line.rstrip())
        if not line.strip():
            continue
        if "\t" in line[: len(line) - len(line.lstrip(" "))]:
            raise SeedValidationError("tabs are not supported in seed YAML indentation")
        indent = len(line) - len(line.lstrip(" "))
        lines.append(_YamlLine(indent=indent, text=line.strip()))
    return lines


def _strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line


def _parse_yaml_block(lines: list[_YamlLine], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    if lines[index].indent < indent:
        return {}, index
    if lines[index].text.startswith("- "):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_map(lines, index, indent)


def _parse_yaml_map(
    lines: list[_YamlLine],
    index: int,
    indent: int,
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent != indent or line.text.startswith("- "):
            break
        key, raw_value = _split_yaml_key_value(line.text)
        index += 1
        if raw_value == "":
            if index < len(lines) and lines[index].indent > indent:
                value, index = _parse_yaml_block(lines, index, lines[index].indent)
            else:
                value = {}
        else:
            value = _parse_yaml_scalar(raw_value)
        result[key] = value
    return result, index


def _parse_yaml_list(
    lines: list[_YamlLine],
    index: int,
    indent: int,
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent != indent or not line.text.startswith("- "):
            break
        item_text = line.text[2:].strip()
        index += 1
        if item_text == "":
            if index < len(lines) and lines[index].indent > indent:
                item, index = _parse_yaml_block(lines, index, lines[index].indent)
            else:
                item = None
        elif _looks_like_inline_mapping(item_text):
            key, raw_value = _split_yaml_key_value(item_text)
            item = {key: _parse_yaml_scalar(raw_value)} if raw_value else {key: {}}
            if index < len(lines) and lines[index].indent > indent:
                nested, index = _parse_yaml_block(lines, index, lines[index].indent)
                if not isinstance(nested, Mapping):
                    raise SeedValidationError("list item continuation must be an object")
                item.update(nested)
        else:
            item = _parse_yaml_scalar(item_text)
        result.append(item)
    return result, index


def _looks_like_inline_mapping(text: str) -> bool:
    if ":" not in text:
        return False
    if text[0] in {"'", '"'}:
        return False
    key, _separator, _value = text.partition(":")
    return bool(key.strip())


def _split_yaml_key_value(text: str) -> tuple[str, str]:
    key, separator, value = text.partition(":")
    if separator != ":" or not key.strip():
        raise SeedValidationError(f"invalid YAML mapping line: {text}")
    return key.strip(), value.strip()


def _parse_yaml_scalar(value: str) -> Any:
    if value == "":
        return ""
    lowered = value.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value[0] == value[-1:] and value[0] in {"'", '"'}:
        if value[0] == '"':
            return json.loads(value)
        return value[1:-1].replace("''", "'")
    if value[0] in "[{":
        return json.loads(value)
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


__all__ = [
    "SeedApplyResult",
    "SeedDocument",
    "SeedValidationError",
    "apply_capability_seed_files",
    "apply_seed_data",
    "apply_seed_files",
    "load_seed_documents",
    "seed_files_from_paths",
]
