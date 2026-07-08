"""18-check RunPod audit harness for Pitwall CI.

Automated, hermetic CI check suite. Each check validates a single
invariant from spec §4.3 / §B.1.

The harness exposes a CLI via ``python -m pitwall.audit.sixteen_check``
that prints a pass/fail report and exits 0 only when all 18 checks pass.

Check functions accept a *config* dict that represents the Pitwall runtime
configuration. In production this is built from environment / DB; in CI
the test suite provides synthetic configs to exercise each check without
live RunPod calls.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from pitwall.runpod_client import pods, templates
from pitwall.runpod_client.gpu import CANONICAL_GPU_NAMES
from pitwall.runpod_client.registry import (
    DOCKER_HUB_PREFIX,
    GHCR_PREFIX,
    GITLAB_REGISTRY_PREFIX,
    registry_auth_id_from_env,
)

SYNC_RESULT_RETENTION_S = 60
ASYNC_RESULT_RETENTION_S = 1800
REQUIRED_DISK_GB_BY_WORKLOAD = {
    "vllm": 80,
    "embed": 40,
    "slim": 20,
}
REQUIRED_REGISTRY_PREFIXES = (
    GHCR_PREFIX,
    GITLAB_REGISTRY_PREFIX,
    DOCKER_HUB_PREFIX,
)
KILL_SWITCH_STEPS = ("list_pods", "terminate_all", "verify")
DEPRECATED_HF_CLI_COMMAND = " ".join(("huggingface-cli", "download"))
VLLM_PROVIDER_TYPES = {"pod_lease", "serverless_lb", "serverless_queue"}
POD_LEASE_PROVIDER_TYPE = "pod_lease"
POD_LEASE_REQUIRED_READINESS_SIGNALS = ("runtime", "port_mappings", "probe_2xx")
MAX_VOLUME_ATTACH_TIMEOUT_S = 300
R2_TEMP_CREDENTIAL_ROUTE_FRAGMENT = "temp-access-credentials"
R2_FORBIDDEN_POD_ENV_KEYS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "R2_ACCESS_KEY",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_KEY",
        "R2_SECRET_ACCESS_KEY",
    }
)
R2_TEMP_CREDENTIAL_STRATEGIES = frozenset(
    {
        "temp",
        "temporary",
        "temporary_credentials",
        "temp_credentials",
        "temp-access-credentials",
        "temporary-credentials",
        "temp-credentials",
        "r2_temp_credentials",
        "r2-temp-credentials",
    }
)
LEASE_STOP_ROUTE_PATH = "/v1/leases/{lease_id}/stop"
ADMIN_KILL_SWITCH_ROUTE_PATH = "/v1/admin/kill-switch"
_MISSING = object()
EXPECTED_AUDIT_CHECK_COUNT = 19
_PRE_SPEND_FINDING_LIMIT = 32
_REDACTED_SECRET = "[REDACTED:secret]"
_REDACTED_PRIVATE_KEY = "[REDACTED:private_key]"
_REDACTED_EMAIL = "[REDACTED:email]"
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_LABELED_CLOUD_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:AWS_(?:SECRET_ACCESS_KEY|SECRET_KEY|SESSION_TOKEN|SECURITY_TOKEN|ACCESS_TOKEN)"
    r"|R2_(?:SECRET_ACCESS_KEY|SECRET_KEY|SESSION_TOKEN|ACCESS_KEY|ACCESS_TOKEN))\b"
    r"\s*(?:=|:)\s*(?:\"[^\"\s]{8,}\"|'[^'\s]{8,}'|[-A-Za-z0-9._/+=:]{8,})"
)
_SECRET_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private_key", _PRIVATE_KEY_RE, _REDACTED_PRIVATE_KEY),
    (
        "labeled_cloud_secret_assignment",
        _LABELED_CLOUD_SECRET_ASSIGNMENT_RE,
        _REDACTED_SECRET,
    ),
    ("openai_style_token", re.compile(r"\bsk-[A-Za-z0-9._-]{16,}\b"), _REDACTED_SECRET),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), _REDACTED_SECRET),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), _REDACTED_SECRET),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), _REDACTED_SECRET),
    ("slack_token", re.compile(r"\bxox[baprs]?-[A-Za-z0-9-]{20,}\b"), _REDACTED_SECRET),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED_SECRET),
    (
        "bearer_token",
        re.compile(r"(?i)(?<=\bBearer )[A-Za-z0-9._-]{20,}\b"),
        _REDACTED_SECRET,
    ),
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"(?![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
)
_PATH_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer_token",
        "credential",
        "credentials",
        "password",
        "secret",
        "secret_key",
        "token",
    }
)
_NON_SECRET_FIELD_NAMES = frozenset(
    {
        "capability",
        "capability_id",
        "capability_name",
        "completion_tokens",
        "dry_run",
        "idempotency_key",
        "input_tokens",
        "max_completion_tokens",
        "max_new_tokens",
        "max_output_tokens",
        "max_tokens",
        "output_tokens",
        "prompt_tokens",
        "provider_id",
        "return_colbert",
        "return_dense",
        "return_sparse",
    }
)


class AuditSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PreSpendDecision(StrEnum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


class PreSpendFindingKind(StrEnum):
    SECRET = "secret"
    PII = "pii"


@dataclass(frozen=True, slots=True)
class PreSpendFinding:
    kind: PreSpendFindingKind
    rule: str
    path: str
    action: PreSpendDecision
    redacted_preview: str
    fingerprint_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "rule": self.rule,
            "path": self.path,
            "action": self.action.value,
            "redacted_preview": self.redacted_preview,
            "fingerprint_sha256": self.fingerprint_sha256,
        }


@dataclass(frozen=True, slots=True)
class PreSpendPayloadScanResult:
    decision: PreSpendDecision
    findings: tuple[PreSpendFinding, ...]
    redacted_payload: Any

    @property
    def blocked(self) -> bool:
        return self.decision == PreSpendDecision.BLOCK

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "blocked": self.blocked,
            "findings": [finding.to_dict() for finding in self.findings],
            "redacted_payload": self.redacted_payload,
        }


class CheckFailed(Exception):
    """Raised by an individual check when its invariant is violated."""

    def __init__(
        self,
        check_id: int,
        message: str,
        severity: AuditSeverity = AuditSeverity.HIGH,
        evidence: str | None = None,
        remediation: str | None = None,
    ) -> None:
        self.check_id = check_id
        self.message = message
        self.severity = severity
        self.evidence = evidence or message
        self.remediation = remediation or ""
        super().__init__(f"Check {check_id}: {message}")


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: int
    name: str
    passed: bool
    severity: AuditSeverity
    evidence: str
    remediation: str
    message: str = ""


class AuditConfig(Protocol):
    """Minimal interface needed by check functions.

    Implementations can pull from env vars, a database, or a hardcoded
    test fixture. This keeps checks decoupled from infrastructure.
    """

    def get(self, key: str, default: Any = None) -> Any: ...

    def gpu_ids(self) -> list[str]: ...

    def workloads(self) -> list[dict[str, Any]]: ...

    def launch_params(self) -> dict[str, Any]: ...

    def readiness_config(self) -> dict[str, Any]: ...

    def cost_config(self) -> dict[str, Any]: ...

    def timeout_config(self) -> dict[str, Any]: ...

    def webhook_config(self) -> dict[str, Any]: ...

    def retention_config(self) -> dict[str, Any]: ...

    def volume_config(self) -> dict[str, Any]: ...

    def probe_config(self) -> dict[str, Any]: ...

    def image_config(self) -> dict[str, Any]: ...

    def disk_config(self) -> dict[str, Any]: ...

    def template_config(self) -> dict[str, Any]: ...

    def registry_config(self) -> dict[str, Any]: ...

    def pre_spend_payloads(self) -> list[dict[str, Any]]: ...

    def provider_fixtures(self) -> list[Any]: ...

    def terminate_config(self) -> dict[str, Any]: ...

    def kill_switch_config(self) -> dict[str, Any]: ...


CHECK_DESCRIPTIONS: dict[int, str] = {
    1: "GPU IDs are canonical RunPod names",
    2: "cloud_type=ALL is never combined with networkVolumeId",
    3: "Pod readiness verified via runtime != null",
    4: "Cost-cap check fires before readiness wait",
    5: "executionTimeout respected with explicit max",
    6: "ttl >= executionTimeout + expected_queue_time",
    7: "Webhook receiver is idempotent and fast-200",
    8: "Result retention windows respected",
    9: "Network-volume DC pin enforced",
    10: "SSH-first probe pattern available for pod-mode readiness",
    11: "Image-pull timeout enforced and staging store wiring is abstracted",
    12: "Container disk explicitly sized per workload; vLLM fixtures use hf download",
    13: "Template create + cache pattern (no template-recreate on every launch)",
    14: "Registry-auth-id selected per image-ref prefix; vLLM fixtures hibernated",
    15: "terminate_* calls are idempotent (404 = success)",
    16: "Kill switch is atomic, 3-step, <30s",
    17: "Pre-spend payload secret guardrail blocks API keys, tokens, and private keys",
    18: "Pre-spend payload PII guardrail redacts emails before spend",
    19: "Policy-as-Code audit gate allows capability, provider, and workload configs",
}


def _bool_config(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_config(check_id: int, name: str, value: Any) -> int:
    if value is None:
        raise CheckFailed(check_id, f"{name} not set")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CheckFailed(check_id, f"{name} must be an integer") from exc
    return parsed


def _float_config(check_id: int, name: str, value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise CheckFailed(check_id, f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CheckFailed(check_id, f"{name} must be a number") from exc
    if not math.isfinite(parsed):
        raise CheckFailed(check_id, f"{name} must be finite")
    return parsed


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return []


def scan_pre_spend_payload(
    payload: Any,
    *,
    max_findings: int = _PRE_SPEND_FINDING_LIMIT,
) -> PreSpendPayloadScanResult:
    """Scan and redact a pre-spend payload without exposing raw matches."""

    findings: list[PreSpendFinding] = []
    decisions: set[PreSpendDecision] = set()
    redacted_payload = _scan_pre_spend_value(
        payload,
        path="$",
        field_name=None,
        findings=findings,
        decisions=decisions,
        max_findings=max_findings,
    )
    has_block = PreSpendDecision.BLOCK in decisions
    has_redact = PreSpendDecision.REDACT in decisions
    decision = (
        PreSpendDecision.BLOCK
        if has_block
        else PreSpendDecision.REDACT
        if has_redact
        else PreSpendDecision.ALLOW
    )
    return PreSpendPayloadScanResult(
        decision=decision,
        findings=tuple(findings),
        redacted_payload=redacted_payload,
    )


def _scan_pre_spend_value(
    value: Any,
    *,
    path: str,
    field_name: str | None,
    findings: list[PreSpendFinding],
    decisions: set[PreSpendDecision],
    max_findings: int,
) -> Any:
    if isinstance(value, str):
        return _scan_pre_spend_string(
            value,
            path=path,
            field_name=field_name,
            findings=findings,
            decisions=decisions,
            max_findings=max_findings,
        )
    if isinstance(value, Mapping):
        redacted_mapping: dict[str, Any] = {}
        for key in sorted(value, key=lambda item: str(item)):
            key_text = str(key)
            redacted_mapping[key_text] = _scan_pre_spend_value(
                value[key],
                path=_child_path(path, key_text),
                field_name=key_text,
                findings=findings,
                decisions=decisions,
                max_findings=max_findings,
            )
        return redacted_mapping
    if isinstance(value, list | tuple):
        return [
            _scan_pre_spend_value(
                item,
                path=f"{path}[{index}]",
                field_name=None,
                findings=findings,
                decisions=decisions,
                max_findings=max_findings,
            )
            for index, item in enumerate(value)
        ]
    return value


def _scan_pre_spend_string(
    value: str,
    *,
    path: str,
    field_name: str | None,
    findings: list[PreSpendFinding],
    decisions: set[PreSpendDecision],
    max_findings: int,
) -> str:
    if _is_secret_field_name(field_name) and value.strip():
        _record_pre_spend_finding(
            findings,
            decisions=decisions,
            max_findings=max_findings,
            kind=PreSpendFindingKind.SECRET,
            rule="secret_field",
            path=path,
            action=PreSpendDecision.BLOCK,
            matched=value,
            redacted_text=_REDACTED_SECRET,
        )
        return _REDACTED_SECRET

    redacted = value
    for rule, pattern, replacement in _SECRET_VALUE_PATTERNS:
        redacted = _redact_pattern(
            redacted,
            pattern=pattern,
            replacement=replacement,
            kind=PreSpendFindingKind.SECRET,
            rule=rule,
            path=path,
            action=PreSpendDecision.BLOCK,
            findings=findings,
            decisions=decisions,
            max_findings=max_findings,
        )

    redacted = _redact_pattern(
        redacted,
        pattern=_EMAIL_RE,
        replacement=_REDACTED_EMAIL,
        kind=PreSpendFindingKind.PII,
        rule="email",
        path=path,
        action=PreSpendDecision.REDACT,
        findings=findings,
        decisions=decisions,
        max_findings=max_findings,
    )
    return redacted


def _redact_pattern(
    value: str,
    *,
    pattern: re.Pattern[str],
    replacement: str,
    kind: PreSpendFindingKind,
    rule: str,
    path: str,
    action: PreSpendDecision,
    findings: list[PreSpendFinding],
    decisions: set[PreSpendDecision],
    max_findings: int,
) -> str:
    def replace_match(match: re.Match[str]) -> str:
        matched = match.group(0)
        _record_pre_spend_finding(
            findings,
            decisions=decisions,
            max_findings=max_findings,
            kind=kind,
            rule=rule,
            path=path,
            action=action,
            matched=matched,
            redacted_text=replacement,
        )
        return replacement

    return pattern.sub(replace_match, value)


def _record_pre_spend_finding(
    findings: list[PreSpendFinding],
    *,
    decisions: set[PreSpendDecision],
    max_findings: int,
    kind: PreSpendFindingKind,
    rule: str,
    path: str,
    action: PreSpendDecision,
    matched: str,
    redacted_text: str,
) -> None:
    decisions.add(action)
    if len(findings) >= max_findings:
        return
    findings.append(
        PreSpendFinding(
            kind=kind,
            rule=rule,
            path=path,
            action=action,
            redacted_preview=_preview(redacted_text),
            fingerprint_sha256=hashlib.sha256(matched.encode("utf-8")).hexdigest(),
        )
    )


def _preview(value: str) -> str:
    max_preview_chars = 96
    if len(value) <= max_preview_chars:
        return value
    return value[: max_preview_chars - 3] + "..."


def _is_secret_field_name(field_name: str | None) -> bool:
    if field_name is None:
        return False
    normalized = field_name.strip().lower().replace("-", "_")
    if normalized in _NON_SECRET_FIELD_NAMES:
        return False
    if normalized in _SECRET_FIELD_NAMES:
        return True
    return any(
        token in normalized
        for token in (
            "api_key",
            "access_key",
            "auth_token",
            "bearer",
            "credential",
            "password",
            "secret",
        )
    )


def _child_path(parent: str, key: str) -> str:
    if _PATH_IDENTIFIER_RE.fullmatch(key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


def _pre_spend_payloads(cfg: AuditConfig) -> list[Any]:
    pre_spend_payloads = getattr(cfg, "pre_spend_payloads", None)
    if callable(pre_spend_payloads):
        return list(pre_spend_payloads())
    return _object_list(cfg.get("pre_spend_payloads", []))


def _findings_evidence(findings: list[PreSpendFinding]) -> str:
    return json.dumps(
        [finding.to_dict() for finding in findings],
        sort_keys=True,
        separators=(",", ":"),
    )


def _workload_gpu_ids(cfg: AuditConfig) -> list[str]:
    gpus: list[str] = []
    for workload in cfg.workloads():
        gpu_types = workload.get("gpu_types") or workload.get("gpuTypeIds")
        gpus.extend(_str_list(gpu_types))
    return gpus


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _python_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python", exclude_none=True)
    return value


def _object_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        if {"id", "name", "provider_type", "config"} & set(value):
            return [value]
        return list(value.values())
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


def _provider_fixtures(cfg: AuditConfig) -> list[Any]:
    provider_fixtures = getattr(cfg, "provider_fixtures", None)
    if callable(provider_fixtures):
        return _object_list(provider_fixtures())
    return _object_list(cfg.get("provider_fixtures", []))


def _provider_config(provider: Any) -> Mapping[str, Any]:
    config = _python_value(_field(provider, "config", {}))
    if isinstance(config, Mapping):
        return config
    return {}


def _provider_type(provider: Any) -> str:
    raw = _field(provider, "provider_type", "")
    value = getattr(raw, "value", raw)
    return str(value)


def _provider_label(provider: Any) -> str:
    for field_name in ("id", "provider_id", "name", "endpoint_id", "runpod_endpoint_id"):
        value = _field(provider, field_name, None)
        if value:
            return str(value)
    return "<unknown provider>"


def _nested_value(value: Any, path: tuple[str, ...]) -> Any:
    current = _python_value(value)
    for key in path:
        current = _python_value(current)
        current = _field(current, key, _MISSING)
        if current is _MISSING:
            return _MISSING
    return current


def _provider_text_value(provider: Any, config: Mapping[str, Any], path: tuple[str, ...]) -> str:
    value = _nested_value(provider if path[0] in {"id", "name", "capability_id"} else config, path)
    if value is _MISSING or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple | set):
        return " ".join(str(item) for item in value)
    return str(value)


def _is_vllm_provider_fixture(provider: Any) -> bool:
    provider_type = _provider_type(provider)
    if provider_type not in VLLM_PROVIDER_TYPES:
        return False

    config = _provider_config(provider)
    markers = [
        _provider_text_value(provider, config, ("id",)),
        _provider_text_value(provider, config, ("name",)),
        _provider_text_value(provider, config, ("capability_id",)),
        _provider_text_value(provider, config, ("image_ref",)),
        _provider_text_value(provider, config, ("image_name",)),
        _provider_text_value(provider, config, ("template_name",)),
        _provider_text_value(provider, config, ("worker_image", "image_ref")),
        _provider_text_value(provider, config, ("env_vars", "VLLM_MODEL")),
        _provider_text_value(provider, config, ("env_vars", "MODEL_NAME")),
        _provider_text_value(provider, config, ("env_vars", "OPENAI_SERVED_MODEL_NAME_OVERRIDE")),
        _provider_text_value(provider, config, ("env", "VLLM_MODEL")),
        _provider_text_value(provider, config, ("env", "MODEL_NAME")),
        _provider_text_value(provider, config, ("env", "OPENAI_SERVED_MODEL_NAME_OVERRIDE")),
        _provider_text_value(provider, config, ("model",)),
        _provider_text_value(provider, config, ("model_name",)),
    ]
    lower_markers = [marker.lower() for marker in markers if marker]
    return any(
        "vllm" in marker or "llm" in marker or "qwen" in marker or "openai-compatible" in marker
        for marker in lower_markers
    )


def _vllm_provider_fixtures(cfg: AuditConfig) -> list[Any]:
    return [provider for provider in _provider_fixtures(cfg) if _is_vllm_provider_fixture(provider)]


def _pod_lease_provider_fixtures(cfg: AuditConfig) -> list[Any]:
    return [
        provider
        for provider in _provider_fixtures(cfg)
        if _provider_type(provider) == POD_LEASE_PROVIDER_TYPE
    ]


def _provider_config_value(provider: Any, paths: tuple[tuple[str, ...], ...]) -> Any:
    config = _provider_config(provider)
    for path in paths:
        value = _nested_value(config, path)
        if value is not _MISSING:
            return value
    return _MISSING


def _normalized_readiness_signal(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    normalized = normalized.replace(" ", "_")
    if normalized in {"runtime", "runtime_present", "runtime_non_null", "runtime_seen"}:
        return "runtime"
    if normalized in {
        "ports",
        "port",
        "port_mappings",
        "portmappings",
        "port_mappings_present",
        "ports_present",
    }:
        return "port_mappings"
    if normalized in {
        "probe",
        "probe_2xx",
        "probe_passed",
        "proxy_or_ssh_probe",
        "ssh_or_proxy_probe",
        "readiness_probe",
    }:
        return "probe_2xx"
    return normalized


def _provider_readiness_signals(provider: Any) -> set[str] | None:
    raw = _provider_config_value(
        provider,
        (
            ("readiness", "required_signals"),
            ("readiness", "signals"),
            ("readiness_signals",),
            ("required_readiness_signals",),
            ("audit", "readiness_signals"),
            ("audit", "required_readiness_signals"),
        ),
    )
    if raw is _MISSING:
        return None
    return {_normalized_readiness_signal(signal) for signal in _str_list(raw)}


def _provider_order(provider: Any, paths: tuple[tuple[str, ...], ...]) -> list[str] | None:
    raw = _provider_config_value(provider, paths)
    if raw is _MISSING:
        return None
    return [step.strip().lower() for step in _str_list(raw) if step.strip()]


def _provider_volume_id(provider: Any) -> str:
    raw = _provider_config_value(
        provider,
        (
            ("network_volume_id",),
            ("networkVolumeId",),
            ("volume_id",),
            ("volume", "id"),
            ("network_volume", "id"),
        ),
    )
    if raw is _MISSING or raw is None:
        return ""
    return str(raw).strip()


def _provider_data_center_ids(provider: Any) -> list[str]:
    raw = _provider_config_value(
        provider,
        (
            ("dataCenterIds",),
            ("data_center_ids",),
            ("data_centers",),
            ("data_center_id",),
            ("datacenter_id",),
        ),
    )
    dc_ids = [dc_id for dc_id in _str_list(raw) if dc_id.strip()] if raw is not _MISSING else []
    if dc_ids:
        return dc_ids
    region = _field(provider, "region", None)
    return [str(region).strip()] if isinstance(region, str) and region.strip() else []


def _provider_attach_timeout_s(check_id: int, provider: Any) -> float:
    raw = _provider_config_value(
        provider,
        (
            ("constraints", "max_attach_hang_s"),
            ("constraints", "volume_attach_timeout_s"),
            ("constraints", "attach_timeout_s"),
            ("max_attach_hang_s",),
            ("volume_attach_timeout_s",),
            ("attach_timeout_s",),
        ),
    )
    if raw is _MISSING:
        return pods.DEFAULT_VOLUME_ATTACH_TIMEOUT_S
    return _float_config(
        check_id,
        f"provider[{_provider_label(provider)}].max_attach_hang_s",
        raw,
    )


def _provider_env_mappings(provider: Any) -> list[Mapping[str, Any]]:
    mappings: list[Mapping[str, Any]] = []
    for path in (
        ("env_vars",),
        ("env",),
        ("environment",),
        ("worker_env",),
        ("pod_env",),
    ):
        raw = _provider_config_value(provider, (path,))
        if isinstance(raw, Mapping):
            mappings.append(raw)
    return mappings


def _provider_requires_r2(provider: Any) -> bool:
    raw = _provider_config_value(
        provider,
        (
            ("requires_r2",),
            ("r2_required",),
            ("r2", "required"),
            ("r2", "enabled"),
            ("storage", "requires_r2"),
        ),
    )
    if raw is _MISSING:
        return False
    return _bool_config(raw)


def _provider_r2_strategy(provider: Any) -> str:
    raw = _provider_config_value(
        provider,
        (
            ("r2", "credential_strategy"),
            ("r2", "strategy"),
            ("r2", "mode"),
            ("r2_credential_strategy",),
            ("storage", "r2_credential_strategy"),
        ),
    )
    if raw is _MISSING or raw is None:
        return ""
    return str(raw).strip().lower().replace("_", "-")


def _provider_container_disk_gb(provider: Any) -> Any:
    config = _provider_config(provider)
    for path in (
        ("container_disk_gb",),
        ("containerDiskInGb",),
        ("container_disk_in_gb",),
        ("template", "container_disk_gb"),
        ("worker_image", "container_disk_gb"),
    ):
        value = _nested_value(config, path)
        if value is not _MISSING:
            return value
    return _MISSING


def _provider_workers_min(provider: Any) -> Any:
    config = _provider_config(provider)
    for path in (
        ("workers_min",),
        ("workersMin",),
        ("workers", "workers_min"),
        ("workers", "workersMin"),
        ("scaling", "workers_min"),
        ("scaling", "workersMin"),
    ):
        value = _nested_value(config, path)
        if value is not _MISSING:
            return value
    for attr in ("workers_min", "workersMin"):
        value = _field(provider, attr, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _provider_hf_command_text(provider: Any) -> str:
    config = _provider_config(provider)
    texts: list[str] = []
    for path in (
        ("download_command",),
        ("hf_download_command",),
        ("preload_command",),
        ("entrypoint",),
        ("docker_entrypoint",),
        ("command",),
        ("worker_image", "download_command"),
        ("worker_image", "entrypoint"),
        ("worker_image", "docker_entrypoint"),
    ):
        text = _provider_text_value(provider, config, path)
        if text:
            texts.append(text)
    return "\n".join(texts)


def _check_order_has_cost_before_readiness(
    *,
    check_id: int,
    order: list[str],
    label: str,
) -> None:
    cost_pos = None
    ready_pos = None
    for i, step in enumerate(order):
        if "cost" in step and cost_pos is None:
            cost_pos = i
        if ("readi" in step or "wait_for_pod_runtime" in step or "probe" in step) and (
            ready_pos is None
        ):
            ready_pos = i
    if cost_pos is None:
        raise CheckFailed(check_id, f"{label} cost-cap check missing from order")
    if ready_pos is None:
        raise CheckFailed(check_id, f"{label} readiness wait missing from order")
    if cost_pos > ready_pos:
        raise CheckFailed(
            check_id,
            f"{label} cost-cap check fires after readiness wait — must fire before",
        )


def _check_pod_lease_readiness_cases(cfg: AuditConfig) -> None:
    partial = pods._ReadinessSignals(
        runtime_seen_at="2026-05-28T12:00:00Z",
        port_mappings_seen_at="2026-05-28T12:00:01Z",
    )
    if partial.complete:
        raise CheckFailed(3, "runtime+port mappings were treated as active without probe 2xx")

    complete = pods._ReadinessSignals(
        runtime_seen_at="2026-05-28T12:00:00Z",
        port_mappings_seen_at="2026-05-28T12:00:01Z",
        probe_passed_at="2026-05-28T12:00:02Z",
        probe_method=pods.SSH_LOCALHOST_PROBE_METHOD,
    )
    if not complete.complete:
        raise CheckFailed(3, "complete pod readiness signals were not treated as active")

    required = set(POD_LEASE_REQUIRED_READINESS_SIGNALS)
    for provider in _pod_lease_provider_fixtures(cfg):
        provider_label = _provider_label(provider)
        signals = _provider_readiness_signals(provider)
        if signals is None:
            raise CheckFailed(
                3,
                f"pod_lease provider fixture {provider_label!r} missing readiness signals",
            )
        missing = [
            signal for signal in POD_LEASE_REQUIRED_READINESS_SIGNALS if signal not in signals
        ]
        if missing:
            raise CheckFailed(
                3,
                f"pod_lease provider fixture {provider_label!r} readiness missing {missing!r}; "
                f"required {sorted(required)!r}",
            )


def _check_pod_lease_cost_order_cases(cfg: AuditConfig) -> None:
    for provider in _pod_lease_provider_fixtures(cfg):
        provider_label = _provider_label(provider)
        order = _provider_order(
            provider,
            (
                ("cost_check_order",),
                ("check_order",),
                ("launch_order",),
                ("audit", "cost_check_order"),
                ("audit", "launch_order"),
            ),
        )
        if order is None:
            raise CheckFailed(
                4,
                f"pod_lease provider fixture {provider_label!r} missing cost/readiness order",
            )
        _check_order_has_cost_before_readiness(
            check_id=4,
            order=order,
            label=f"pod_lease provider fixture {provider_label!r}",
        )


def _check_pod_lease_attach_hang_cases(cfg: AuditConfig) -> None:
    if pods.DEFAULT_VOLUME_ATTACH_TIMEOUT_S > MAX_VOLUME_ATTACH_TIMEOUT_S:
        raise CheckFailed(
            9,
            "default volume attach timeout exceeds 5-minute L7 budget",
        )
    source = "\n".join(
        (
            inspect.getsource(pods.wait_for_pod_runtime_sync),
            inspect.getsource(pods._wait_for_pod_runtime_sync),
        )
    )
    for required_token in (
        "_pod_has_network_volume",
        "_pod_has_zero_uptime",
        "PodVolumeAttachTimeout",
    ):
        if required_token not in source:
            raise CheckFailed(
                9,
                f"wait_for_pod_runtime_sync missing L7 attach-hang guard {required_token}",
            )

    from pitwall.api.leases import launch as lease_launch

    launch_source = "\n".join(
        (
            inspect.getsource(lease_launch._run_launch_runpod),
            inspect.getsource(lease_launch._set_provider_attach_hang_cooldown),
        )
    )
    for required_token in (
        "ProviderAttachHangRecoveryRequested",
        "_set_provider_attach_hang_cooldown",
    ):
        if required_token not in launch_source:
            raise CheckFailed(
                9,
                f"pod lease launch missing attach-hang recovery path {required_token}",
            )

    for provider in _pod_lease_provider_fixtures(cfg):
        if not _provider_volume_id(provider):
            continue
        provider_label = _provider_label(provider)
        dc_ids = _provider_data_center_ids(provider)
        if len(dc_ids) != 1:
            raise CheckFailed(
                9,
                f"pod_lease provider fixture {provider_label!r} has volume with "
                f"{len(dc_ids)} data centers; expected exactly one",
            )
        timeout_s = _provider_attach_timeout_s(9, provider)
        if timeout_s <= 0:
            raise CheckFailed(
                9,
                f"pod_lease provider fixture {provider_label!r} attach timeout must be > 0",
            )
        if timeout_s > MAX_VOLUME_ATTACH_TIMEOUT_S:
            raise CheckFailed(
                9,
                f"pod_lease provider fixture {provider_label!r} attach timeout "
                f"{timeout_s:g}s exceeds {MAX_VOLUME_ATTACH_TIMEOUT_S}s",
            )


def _check_r2_temp_credential_cases(cfg: AuditConfig) -> None:
    from pitwall.api.leases import launch as lease_launch
    from pitwall.r2_temp_credentials import CloudflareR2TempCredentialClient, R2TemporaryCredentials
    from pitwall.staging_store import (
        CloudflareR2StagingStore,
        NoOpStagingStore,
        get_staging_store,
    )

    client_source = inspect.getsource(CloudflareR2TempCredentialClient.create)
    if R2_TEMP_CREDENTIAL_ROUTE_FRAGMENT not in client_source:
        raise CheckFailed(11, "R2 client does not call temp-access-credentials endpoint")
    deprecated_rotation_fragment = "/" + "/".join(("r2", "tokens"))
    env_for_pod_source = inspect.getsource(lease_launch._env_for_pod)
    combined_source = "\n".join(
        (
            client_source,
            env_for_pod_source,
        )
    )
    if deprecated_rotation_fragment in combined_source:
        raise CheckFailed(11, "R2 deprecated token-rotation endpoint appears in pod path")
    if (
        "vend_pod_credentials" not in env_for_pod_source
        or "get_staging_store" not in env_for_pod_source
    ):
        raise CheckFailed(11, "pod lease env path does not use the StagingStore seam")
    if "vend_r2_temp_credential_pod_env" in env_for_pod_source:
        raise CheckFailed(11, "pod lease env path calls R2 temporary credentials directly")

    default_store = get_staging_store(environ={})
    if not isinstance(default_store, NoOpStagingStore):
        raise CheckFailed(11, "StagingStore default is not no-op when R2 is unconfigured")
    if default_store.vend_pod_credentials() != {}:
        raise CheckFailed(11, "NoOpStagingStore vends pod credentials")
    if default_store.cleanup_pod_artifacts([{"id": "pod-1", "name": "pod"}]) != []:
        raise CheckFailed(11, "NoOpStagingStore cleanup is not empty")

    r2_store_source = "\n".join(
        (
            inspect.getsource(CloudflareR2StagingStore.vend_pod_credentials),
            inspect.getsource(CloudflareR2StagingStore.cleanup_pod_artifacts),
        )
    )
    if "vend_r2_temp_credential_pod_env" not in r2_store_source:
        raise CheckFailed(11, "Cloudflare R2 staging store does not wrap temp credential vending")
    if "cleanup_staging_for_pods" not in r2_store_source:
        raise CheckFailed(11, "Cloudflare R2 staging store does not wrap staging cleanup")

    credential = R2TemporaryCredentials(
        access_key_id="tmp-access",
        secret_access_key="tmp-secret",
        session_token="tmp-session",
        ttl_seconds=900,
        bucket="pitwall-staging",
        permission="object-read-write",
    )
    env = credential.as_pod_env(endpoint="https://r2.example.test")
    if (
        env.get("AWS_SESSION_TOKEN") != "tmp-session"
        or env.get("R2_SESSION_TOKEN") != "tmp-session"
    ):
        raise CheckFailed(11, "R2 temporary credential pod env is missing session tokens")
    if "R2_ACCESS_KEY" in env or "R2_SECRET_KEY" in env:
        raise CheckFailed(11, "R2 pod env exposes long-lived R2 key names")

    for provider in _pod_lease_provider_fixtures(cfg):
        provider_label = _provider_label(provider)
        for env_mapping in _provider_env_mappings(provider):
            forbidden = sorted(R2_FORBIDDEN_POD_ENV_KEYS & set(env_mapping))
            if forbidden:
                raise CheckFailed(
                    11,
                    f"pod_lease provider fixture {provider_label!r} injects "
                    f"Pitwall-managed R2 credential env keys: {forbidden!r}",
                )

        strategy = _provider_r2_strategy(provider)
        if strategy and strategy not in R2_TEMP_CREDENTIAL_STRATEGIES:
            raise CheckFailed(
                11,
                f"pod_lease provider fixture {provider_label!r} uses non-temporary "
                f"R2 credential strategy {strategy!r}",
            )
        if _provider_requires_r2(provider) and not strategy:
            raise CheckFailed(
                11,
                f"pod_lease provider fixture {provider_label!r} requires R2 but "
                "does not declare a temporary credential strategy",
            )


def _router_has_method(router: Any, path: str, method: str) -> bool:
    for route in getattr(router, "routes", ()):
        route_path = getattr(route, "path", None)
        methods: set[str] = getattr(route, "methods", set())
        if route_path == path and method in methods:
            return True
    return False


def _check_l15_verb_separation() -> None:
    from pitwall.api.admin import emergency
    from pitwall.api.routes import leases as lease_routes

    if not _router_has_method(lease_routes.router, LEASE_STOP_ROUTE_PATH, "POST"):
        raise CheckFailed(15, f"single-lease stop route missing: {LEASE_STOP_ROUTE_PATH}")
    if not _router_has_method(emergency.router, ADMIN_KILL_SWITCH_ROUTE_PATH, "POST"):
        raise CheckFailed(
            15,
            f"account-wide kill switch route missing: {ADMIN_KILL_SWITCH_ROUTE_PATH}",
        )
    if LEASE_STOP_ROUTE_PATH == ADMIN_KILL_SWITCH_ROUTE_PATH:
        raise CheckFailed(15, "single-lease stop and account kill switch share a route")
    if "run_teardown" not in inspect.getsource(lease_routes.stop_lease):
        raise CheckFailed(15, "single-lease stop route does not delegate to lease teardown")
    if "persist_kill_report" not in inspect.getsource(emergency.run_kill):
        raise CheckFailed(15, "admin kill switch does not persist an account-wide audit log")
    if "rest:admin" not in inspect.getsource(emergency.activate_kill_switch):
        raise CheckFailed(15, "admin kill switch route does not use the admin audit actor")


def _check_l16_patch_validation() -> None:
    from pitwall.api.routes import leases as lease_routes
    from pitwall.api.schemas.leases import LeasePatch, lease_patch_conflicting_fields

    if not _router_has_method(lease_routes.router, "/v1/leases/{lease_id}", "PATCH"):
        raise CheckFailed(16, "lease PATCH route missing")

    conflicts = lease_patch_conflicting_fields(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:sha-1",
            "gpuTypeIds": ["NVIDIA L4"],
            "volume_id": "vol-model-cache",
        }
    )
    if conflicts != ["image_ref", "gpuTypeIds", "volume_id"]:
        raise CheckFailed(16, f"lease PATCH multi-axis validation returned {conflicts!r}")

    single_axis = LeasePatch.model_validate(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:sha-1",
            "template_name": "pitwall-qwen3",
        }
    )
    if lease_patch_conflicting_fields(single_axis):
        raise CheckFailed(16, "lease PATCH rejected a single-axis image/template change")

    patch_source = inspect.getsource(lease_routes.patch_lease)
    if not _source_order("lease_patch_conflicting_fields", "patch_lease_settings", patch_source):
        raise CheckFailed(16, "lease PATCH validation does not run before repository access")


def _source_order(first: str, second: str, source: str) -> bool:
    first_index = source.find(first)
    second_index = source.find(second)
    return first_index >= 0 and second_index >= 0 and first_index < second_index


def check_01_gpu_ids_canonical(cfg: AuditConfig) -> str:
    gpus = [*cfg.gpu_ids(), *_workload_gpu_ids(cfg)]
    if not gpus:
        raise CheckFailed(1, "no GPU IDs configured for audit")
    non_canonical = [g for g in gpus if g not in CANONICAL_GPU_NAMES]
    if non_canonical:
        raise CheckFailed(
            1,
            f"non-canonical GPU IDs: {non_canonical!r}",
        )
    return "all GPU IDs are canonical"


def check_02_cloud_type_volume(cfg: AuditConfig) -> str:
    params = cfg.launch_params()
    cloud_type = str(params.get("cloud_type", "")).upper()
    has_volume = bool(params.get("networkVolumeId"))
    if cloud_type == "ALL" and has_volume:
        raise CheckFailed(
            2,
            "cloud_type=ALL combined with networkVolumeId — "
            "COMMUNITY attempt will always fail (volumes are Secure Cloud only)",
        )
    volume_cloud_types = pods._cloud_types_for_rest("ALL", network_volume_id="vol-audit")
    if volume_cloud_types != ["SECURE"]:
        raise CheckFailed(
            2,
            f"launcher expands ALL+networkVolumeId to {volume_cloud_types!r}; expected ['SECURE']",
        )
    return "cloud_type and volume usage are consistent"


def check_03_readiness_runtime(cfg: AuditConfig) -> str:
    rc = cfg.readiness_config()
    probe_field = str(rc.get("probe_field", ""))
    if probe_field != "runtime":
        raise CheckFailed(
            3,
            f"readiness probes '{probe_field}' instead of 'runtime' — "
            "desiredStatus is not sufficient",
        )
    desired_running_without_runtime = {
        "id": "pod-audit",
        "desiredStatus": "RUNNING",
        "runtime": None,
        "portMappings": {"8000": 12345},
    }
    if pods._pod_has_runtime(desired_running_without_runtime):
        raise CheckFailed(3, "desiredStatus=RUNNING was treated as ready without runtime")
    runtime_with_ports = {
        "id": "pod-audit",
        "desiredStatus": "PENDING",
        "runtime": {"ports": [{"privatePort": 8000, "type": "http"}]},
    }
    if not pods._pod_has_runtime(runtime_with_ports):
        raise CheckFailed(3, "runtime+ports was not treated as a readiness signal")
    _check_pod_lease_readiness_cases(cfg)
    return "readiness verified via runtime, port mappings, and probe signals"


def check_04_cost_cap_before_readiness(cfg: AuditConfig) -> str:
    cc = cfg.cost_config()
    order = list(cc.get("check_order", []))
    if not order:
        raise CheckFailed(4, "cost check order not configured")
    _check_order_has_cost_before_readiness(
        check_id=4,
        order=[str(step).lower() for step in order],
        label="runtime config",
    )
    source = "\n".join(
        (
            inspect.getsource(pods.create_pod_with_fallback_sync),
            inspect.getsource(pods._create_pod_with_fallback_sync),
        )
    )
    if not _source_order(
        "_gate_pod_cost_before_readiness",
        "wait_for_pod_runtime_sync",
        source,
    ):
        raise CheckFailed(
            4,
            "create_pod_with_fallback_sync does not gate cost before readiness wait",
        )
    _check_pod_lease_cost_order_cases(cfg)
    return "cost-cap check fires before readiness wait for pod-lease providers"


def check_05_execution_timeout(cfg: AuditConfig) -> str:
    tc = cfg.timeout_config()
    timeout = _int_config(5, "executionTimeout", tc.get("executionTimeout"))
    max_timeout = _int_config(5, "executionTimeoutMax", tc.get("executionTimeoutMax"))
    if timeout <= 0:
        raise CheckFailed(5, "executionTimeout must be > 0")
    if max_timeout <= 0:
        raise CheckFailed(5, "executionTimeoutMax must be > 0")
    if timeout > max_timeout:
        raise CheckFailed(
            5,
            f"executionTimeout ({timeout}) exceeds max ({max_timeout})",
        )
    return f"executionTimeout={timeout} within bounds"


def check_06_ttl_ge_timeout_plus_queue(cfg: AuditConfig) -> str:
    tc = cfg.timeout_config()
    ttl = _int_config(6, "ttl", tc.get("ttl"))
    exec_timeout = _int_config(6, "executionTimeout", tc.get("executionTimeout"))
    queue_time = _int_config(6, "expected_queue_time", tc.get("expected_queue_time"))
    if ttl < exec_timeout + queue_time:
        raise CheckFailed(
            6,
            f"ttl ({ttl}) < executionTimeout ({exec_timeout}) + expected_queue_time ({queue_time})",
        )
    return f"ttl ({ttl}) >= executionTimeout + queue_time"


def check_07_webhook_idempotent_fast200(cfg: AuditConfig) -> str:
    wc = cfg.webhook_config()
    if not _bool_config(wc.get("idempotent")):
        raise CheckFailed(7, "webhook receiver is not idempotent")
    if not _bool_config(wc.get("fast_200")):
        raise CheckFailed(7, "webhook receiver does not fast-200")
    from pitwall.webhook_receiver import app

    post_paths = {
        route.path
        for route in app.routes
        if "POST" in getattr(route, "methods", set()) and hasattr(route, "path")
    }
    if not ({"/webhooks/runpod", "/runpod"} & post_paths):
        raise CheckFailed(7, "webhook receiver has no RunPod POST fast-200 endpoint")
    return "webhook receiver is idempotent and fast-200"


def check_08_retention_windows(cfg: AuditConfig) -> str:
    rc = cfg.retention_config()
    sync_ret = _int_config(8, "sync_retention_s", rc.get("sync_retention_s"))
    async_ret = _int_config(8, "async_retention_s", rc.get("async_retention_s"))
    if sync_ret < SYNC_RESULT_RETENTION_S:
        raise CheckFailed(
            8,
            f"sync retention ({sync_ret}s) < minimum ({SYNC_RESULT_RETENTION_S}s)",
        )
    if async_ret < ASYNC_RESULT_RETENTION_S:
        raise CheckFailed(
            8,
            f"async retention ({async_ret}s) < minimum ({ASYNC_RESULT_RETENTION_S}s)",
        )
    if not _bool_config(rc.get("persist_before_expiry")):
        raise CheckFailed(8, "results are not configured to persist before RunPod expiry")
    sync_deadline = _int_config(
        8,
        "sync_persist_deadline_s",
        rc.get("sync_persist_deadline_s"),
    )
    async_deadline = _int_config(
        8,
        "async_persist_deadline_s",
        rc.get("async_persist_deadline_s"),
    )
    if sync_deadline >= SYNC_RESULT_RETENTION_S:
        raise CheckFailed(
            8,
            f"sync persist deadline ({sync_deadline}s) must be before {SYNC_RESULT_RETENTION_S}s",
        )
    if async_deadline >= ASYNC_RESULT_RETENTION_S:
        raise CheckFailed(
            8,
            f"async persist deadline ({async_deadline}s) must be before {ASYNC_RESULT_RETENTION_S}s",
        )
    return "results persist before retention expiry (sync<60s, async<1800s)"


def check_09_dc_pin(cfg: AuditConfig) -> str:
    vc = cfg.volume_config()
    dc_ids = [dc_id for dc_id in _str_list(vc.get("dataCenterIds", [])) if dc_id.strip()]
    has_volume = bool(vc.get("networkVolumeId"))
    if has_volume and len(dc_ids) != 1:
        raise CheckFailed(
            9,
            f"volume attached with {len(dc_ids)} dataCenterIds — must pin to exactly one DC",
        )
    _check_pod_lease_attach_hang_cases(cfg)
    return "network-volume DC pin and attach-hang timeout enforced"


def check_10_ssh_first_probe(cfg: AuditConfig) -> str:
    pc = cfg.probe_config()
    if not _bool_config(pc.get("ssh_first")):
        raise CheckFailed(
            10,
            "SSH-first probe pattern not enabled for pod-mode readiness",
        )
    probe_methods = _str_list(pc.get("probe_methods"))
    if probe_methods and pods.SSH_LOCALHOST_PROBE_METHOD not in probe_methods:
        raise CheckFailed(10, "ssh_localhost is absent from configured probe methods")
    primary_probe = pc.get("primary_probe")
    if primary_probe is not None and primary_probe != pods.SSH_LOCALHOST_PROBE_METHOD:
        raise CheckFailed(10, f"primary probe is {primary_probe!r}, expected ssh_localhost")
    if pods.POD_READINESS_PROBE_ORDER[0] != pods.SSH_LOCALHOST_PROBE_METHOD:
        raise CheckFailed(10, "pod readiness probe order is not SSH-first")
    return "SSH-first probe pattern available"


def check_11_image_pull_timeout(cfg: AuditConfig) -> str:
    ic = cfg.image_config()
    pull_timeout = _int_config(11, "image_pull_timeout_s", ic.get("image_pull_timeout_s"))
    startup_timeout = _int_config(11, "startup_timeout_s", ic.get("startup_timeout_s"))
    if pull_timeout <= 0:
        raise CheckFailed(11, "image_pull_timeout_s must be > 0")
    if pull_timeout < startup_timeout:
        raise CheckFailed(
            11,
            f"image_pull_timeout ({pull_timeout}s) < startup_timeout ({startup_timeout}s)",
        )
    _check_r2_temp_credential_cases(cfg)
    return f"image-pull timeout ({pull_timeout}s) >= startup_timeout; StagingStore seam verified"


def check_12_disk_sized(cfg: AuditConfig) -> str:
    dc = cfg.disk_config()
    workloads = dc.get("per_workload", {})
    if not workloads:
        raise CheckFailed(12, "container disk not sized per workload")
    for wl_name, required_gb in REQUIRED_DISK_GB_BY_WORKLOAD.items():
        if wl_name not in workloads:
            raise CheckFailed(12, f"missing container disk size for {wl_name!r}")
        size_gb = _int_config(12, f"per_workload[{wl_name}]", workloads.get(wl_name))
        if size_gb < required_gb:
            raise CheckFailed(
                12,
                f"workload {wl_name!r} disk {size_gb}GB < required {required_gb}GB",
            )
    vllm_fixtures = _vllm_provider_fixtures(cfg)
    for provider in vllm_fixtures:
        provider_label = _provider_label(provider)
        disk_gb = _provider_container_disk_gb(provider)
        if disk_gb is _MISSING:
            raise CheckFailed(
                12,
                f"vLLM provider fixture {provider_label!r} missing container_disk_gb",
            )
        parsed_disk_gb = _int_config(
            12,
            f"provider[{provider_label}].container_disk_gb",
            disk_gb,
        )
        required_vllm_disk_gb = REQUIRED_DISK_GB_BY_WORKLOAD["vllm"]
        if parsed_disk_gb < required_vllm_disk_gb:
            raise CheckFailed(
                12,
                f"vLLM provider fixture {provider_label!r} disk {parsed_disk_gb}GB "
                f"< required {required_vllm_disk_gb}GB",
            )
        command_text = _provider_hf_command_text(provider)
        if DEPRECATED_HF_CLI_COMMAND in command_text:
            raise CheckFailed(
                12,
                f"vLLM provider fixture {provider_label!r} uses deprecated "
                f"{DEPRECATED_HF_CLI_COMMAND!r}",
            )
    return "container disk sized for vllm/embed/slim workloads; provider commands verified"


def check_13_template_cache(cfg: AuditConfig) -> str:
    tc = cfg.template_config()
    if not _bool_config(tc.get("cache_enabled")):
        raise CheckFailed(
            13,
            "template cache not enabled — templates recreated on every launch",
        )
    if not _bool_config(tc.get("create_on_cache_miss", True)):
        raise CheckFailed(13, "template creation on cache miss is not configured")
    if not _bool_config(tc.get("reuse_on_cache_hit", True)):
        raise CheckFailed(13, "cached templates are not reused")
    source = inspect.getsource(templates.ensure_template)
    if not _source_order("_lookup_cached", "create_template", source):
        raise CheckFailed(13, "ensure_template does not look up cache before create")
    if "_insert_cache" not in source:
        raise CheckFailed(13, "ensure_template does not persist created templates to cache")
    return "template create + cache pattern enabled"


def check_14_registry_auth(cfg: AuditConfig) -> str:
    rc = cfg.registry_config()
    mapping = rc.get("prefix_to_auth_id", {})
    if not mapping:
        raise CheckFailed(
            14,
            "registry-auth-id not configured for any image-ref prefix",
        )
    missing = [prefix for prefix in REQUIRED_REGISTRY_PREFIXES if prefix not in mapping]
    if missing:
        raise CheckFailed(14, f"registry-auth-id mapping missing prefixes: {missing!r}")
    if not mapping.get(GHCR_PREFIX):
        raise CheckFailed(14, "GHCR image prefix has no registry-auth-id")
    if not mapping.get(GITLAB_REGISTRY_PREFIX):
        raise CheckFailed(14, "GitLab Registry image prefix has no registry-auth-id")

    fake_env = {
        "RUNPOD_REGISTRY_AUTH_ID_GHCR": "ghcr-auth",
        "RUNPOD_REGISTRY_AUTH_ID_GITLAB": "gitlab-auth",
        "RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB": "docker-auth",
        "RUNPOD_REGISTRY_AUTH_ID": "legacy-auth",
    }
    selections = {
        GHCR_PREFIX: registry_auth_id_from_env(
            "ghcr.io/example/pitwall-worker:abc",
            environ=fake_env,
        ),
        GITLAB_REGISTRY_PREFIX: registry_auth_id_from_env(
            "registry.gitlab.com/example/pitwall-worker:abc",
            environ=fake_env,
        ),
        DOCKER_HUB_PREFIX: registry_auth_id_from_env(
            "vllm/vllm-openai:v0.11.2",
            environ=fake_env,
        ),
    }
    expected = {
        GHCR_PREFIX: "ghcr-auth",
        GITLAB_REGISTRY_PREFIX: "gitlab-auth",
        DOCKER_HUB_PREFIX: "docker-auth",
    }
    if selections != expected:
        raise CheckFailed(14, f"registry auth selector returned {selections!r}")
    for provider in _vllm_provider_fixtures(cfg):
        provider_label = _provider_label(provider)
        provider_type = _provider_type(provider)
        if provider_type not in {"serverless_lb", "serverless_queue"}:
            continue
        workers_min = _provider_workers_min(provider)
        if workers_min is _MISSING:
            raise CheckFailed(
                14,
                f"vLLM provider fixture {provider_label!r} missing workers_min for L14",
            )
        parsed_workers_min = _int_config(
            14,
            f"provider[{provider_label}].workers_min",
            workers_min,
        )
        if parsed_workers_min > 0:
            raise CheckFailed(
                14,
                f"vLLM provider fixture {provider_label!r} has workers_min="
                f"{parsed_workers_min}; L14 requires hibernated fixtures",
            )
    return f"registry-auth-id mapped for {len(mapping)} prefix(es); L14 fixtures hibernated"


def check_15_terminate_idempotent(cfg: AuditConfig) -> str:
    tc = cfg.terminate_config()
    if not _bool_config(tc.get("treat_404_as_success")):
        raise CheckFailed(
            15,
            "terminate calls do not treat 404 as success — not idempotent",
        )
    original_rest_request = pods._rest_request

    def fake_404(
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float = 60.0,
        api_key: str | None = None,
        rest_api_url: str | None = None,
    ) -> Any:
        raise pods.RunPodRestError(method, path, 404, "not found")

    try:
        pods._rest_request = fake_404
        pods.terminate_pod_sync("pod-already-gone")
    except pods.RunPodError as exc:
        raise CheckFailed(15, f"terminate_pod_sync raised on 404: {exc}") from exc
    finally:
        pods._rest_request = original_rest_request
    _check_l15_verb_separation()
    return "terminate calls are idempotent; stop and kill-switch verbs are separated"


def check_16_kill_switch_atomic(cfg: AuditConfig) -> str:
    kc = cfg.kill_switch_config()
    if not _bool_config(kc.get("atomic")):
        raise CheckFailed(16, "kill switch is not atomic")
    steps = tuple(_str_list(kc.get("steps", [])))
    if steps != KILL_SWITCH_STEPS:
        raise CheckFailed(
            16,
            f"kill switch steps {steps!r} != {KILL_SWITCH_STEPS!r}",
        )
    budget_s = kc.get("budget_s")
    if budget_s is None:
        raise CheckFailed(16, "kill switch budget_s not configured")
    budget = _int_config(16, "budget_s", budget_s)
    if budget >= 30:
        raise CheckFailed(
            16,
            f"kill switch budget ({budget_s}s) exceeds 30s limit",
        )
    _check_l16_patch_validation()
    return "kill switch is atomic, 3-step, <30s; lease PATCH is single-axis"


def check_17_pre_spend_secret_guardrail(cfg: AuditConfig) -> str:
    token_probe = scan_pre_spend_payload(
        {"prompt": "token=sk-test_1234567890abcdef1234567890abcdef"}
    )
    if token_probe.decision != PreSpendDecision.BLOCK:
        raise CheckFailed(
            17,
            "pre-spend scanner did not block API-token-shaped payload",
            severity=AuditSeverity.CRITICAL,
        )

    private_key_probe = scan_pre_spend_payload(
        {
            "prompt": "\n".join(
                (
                    "-----BEGIN PRIVATE KEY-----",
                    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
                    "-----END PRIVATE KEY-----",
                )
            )
        }
    )
    if private_key_probe.decision != PreSpendDecision.BLOCK:
        raise CheckFailed(
            17,
            "pre-spend scanner did not block private-key material",
            severity=AuditSeverity.CRITICAL,
        )

    for index, payload in enumerate(_pre_spend_payloads(cfg)):
        result = scan_pre_spend_payload(payload)
        secret_findings = [
            finding for finding in result.findings if finding.kind == PreSpendFindingKind.SECRET
        ]
        if result.decision == PreSpendDecision.BLOCK:
            raise CheckFailed(
                17,
                f"pre-spend payload fixture {index} contains secret material",
                severity=AuditSeverity.CRITICAL,
                evidence=_findings_evidence(secret_findings or list(result.findings)),
                remediation=(
                    "Remove secrets from inbound inference/capability payloads; "
                    "pass credentials through configured server-side secret channels only."
                ),
            )
    return "pre-spend secret guardrail blocks API keys, tokens, and private keys"


def check_18_pre_spend_pii_redaction(cfg: AuditConfig) -> str:
    email_probe = scan_pre_spend_payload({"prompt": "contact ada.lovelace@example.com"})
    if email_probe.decision != PreSpendDecision.REDACT:
        raise CheckFailed(
            18,
            "pre-spend scanner did not redact email PII",
            severity=AuditSeverity.HIGH,
        )
    if "ada.lovelace@example.com" in json.dumps(email_probe.to_dict(), sort_keys=True):
        raise CheckFailed(
            18,
            "pre-spend scanner returned raw email PII in structured output",
            severity=AuditSeverity.HIGH,
        )

    for index, payload in enumerate(_pre_spend_payloads(cfg)):
        result = scan_pre_spend_payload(payload)
        pii_findings = [
            finding for finding in result.findings if finding.kind == PreSpendFindingKind.PII
        ]
        if pii_findings:
            raise CheckFailed(
                18,
                f"pre-spend payload fixture {index} contains unredacted PII",
                severity=AuditSeverity.HIGH,
                evidence=_findings_evidence(pii_findings),
                remediation=(
                    "Redact PII before storing audit fixtures or forwarding payloads "
                    "to provider execution paths."
                ),
            )
    return "pre-spend PII guardrail redacts emails before spend"


def check_19_policy_as_code_audit_gate(cfg: AuditConfig) -> str:
    from pitwall.policy import evaluate_default_policies

    result = evaluate_default_policies(cfg)
    if not result.allowed:
        raise CheckFailed(
            19,
            f"policy-as-code audit gate denied {len(result.violations)} finding(s)",
            severity=AuditSeverity.HIGH,
            evidence=json.dumps(
                result.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            remediation=(
                "Update capability, provider, or workload configuration to satisfy "
                "packaged Policy-as-Code rules before invoking RunPod spend paths."
            ),
        )
    return "policy-as-code audit gate allowed configured providers and workloads"


CHECK_FUNCTIONS: list[Callable[[AuditConfig], str]] = [
    check_01_gpu_ids_canonical,
    check_02_cloud_type_volume,
    check_03_readiness_runtime,
    check_04_cost_cap_before_readiness,
    check_05_execution_timeout,
    check_06_ttl_ge_timeout_plus_queue,
    check_07_webhook_idempotent_fast200,
    check_08_retention_windows,
    check_09_dc_pin,
    check_10_ssh_first_probe,
    check_11_image_pull_timeout,
    check_12_disk_sized,
    check_13_template_cache,
    check_14_registry_auth,
    check_15_terminate_idempotent,
    check_16_kill_switch_atomic,
    check_17_pre_spend_secret_guardrail,
    check_18_pre_spend_pii_redaction,
    check_19_policy_as_code_audit_gate,
]

for n, fn in enumerate(CHECK_FUNCTIONS, start=1):
    cast(Any, fn).check_id = n

assert len(CHECK_FUNCTIONS) == EXPECTED_AUDIT_CHECK_COUNT, (
    f"expected {EXPECTED_AUDIT_CHECK_COUNT} checks, got {len(CHECK_FUNCTIONS)}"
)
assert all(hasattr(fn, "check_id") for fn in CHECK_FUNCTIONS), (
    "all check functions must have check_id attribute"
)
assert all(cast(Any, fn).check_id == n for n, fn in enumerate(CHECK_FUNCTIONS, start=1)), (
    "check_id attributes must match enumerate order"
)


def run_all_checks(cfg: AuditConfig) -> list[CheckResult]:
    """Execute all audit checks against *cfg*, returning per-check results."""
    results: list[CheckResult] = []
    for fn in CHECK_FUNCTIONS:
        check_id: int = cast(Any, fn).check_id
        name = CHECK_DESCRIPTIONS.get(check_id) or fn.__name__
        try:
            message = fn(cfg)
            results.append(
                CheckResult(
                    check_id=check_id,
                    name=name,
                    passed=True,
                    severity=AuditSeverity.LOW,
                    evidence=message,
                    remediation="",
                    message=message,
                )
            )
        except CheckFailed as exc:
            results.append(
                CheckResult(
                    check_id=check_id,
                    name=name,
                    passed=False,
                    severity=exc.severity,
                    evidence=exc.evidence,
                    remediation=exc.remediation,
                    message=exc.message,
                )
            )
    return results


def format_report(results: list[CheckResult]) -> str:
    """Format results as a human-readable report."""
    lines: list[str] = []
    passed = sum(1 for r in results if r.passed)
    lines.append(
        f"Pitwall {EXPECTED_AUDIT_CHECK_COUNT}-check audit: {passed}/{len(results)} passed"
    )
    lines.append("")
    for r in results:
        tag = "PASS" if r.passed else "FAIL"
        lines.append(f"  [{r.check_id:2d}] {tag} [{r.severity.value}] {r.name}: {r.message}")
        if not r.passed and r.remediation:
            lines.append(f"         Evidence: {r.evidence}")
            lines.append(f"         Remediation: {r.remediation}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] in ("--help", "-h"):
        print("Usage: python -m pitwall.audit.sixteen_check [--strict] [--json]")
        print()
        print(
            f"Run the {EXPECTED_AUDIT_CHECK_COUNT}-check RunPod audit against Pitwall "
            "configuration."
        )
        print("  --strict  Exit non-zero if any check fails (default for CI)")
        print("  --json    Output results as JSON")
        print("Exits 0 if all checks pass, 1 otherwise.")
        return 0

    output_json = "--json" in args
    strict = "--strict" in args

    from pitwall.audit._runtime_config import RuntimeAuditConfig

    cfg = RuntimeAuditConfig()
    results = run_all_checks(cfg)

    if output_json:
        all_passed = all(r.passed for r in results)
        payload = {
            "all_passed": all_passed,
            "strict": strict,
            "checks": [
                {
                    "check_id": r.check_id,
                    "name": r.name,
                    "passed": r.passed,
                    "severity": r.severity.value,
                    "evidence": r.evidence,
                    "remediation": r.remediation,
                    "message": r.message,
                }
                for r in results
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(format_report(results))
        all_passed = all(r.passed for r in results)

    if strict:
        return 0 if all_passed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
