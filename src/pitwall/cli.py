"""Top-level Pitwall CLI dispatcher.

Usage::

    pitwall-gpu-broker db migrate          Apply pending migrations
    pitwall-gpu-broker db reset            Drop the pitwall schema
    pitwall-gpu-broker db status           Show migration status
    pitwall-gpu-broker init                Guided local onboarding
    pitwall-gpu-broker create-capability   Create or update a capability by name/spec
    pitwall-gpu-broker seed                Apply capability/provider seed files
    pitwall-gpu-broker config check        Validate boot-time configuration
    pitwall-gpu-broker register-template   Register a RunPod template and cache its ID
    pitwall-gpu-broker set-provider-health Mark a provider healthy/unhealthy/hibernated
    pitwall-gpu-broker terminate-pod       Terminate a RunPod pod by id with verification
    pitwall-gpu-broker warm-volume         Pre-warm a volume with capability-specific assets
    pitwall-gpu-broker dashboard           Launch the Textual operator console
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import sys
import time
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from typing import Any, TextIO

from pitwall.cli_output import Output, _safe_json, add_json_argument
from pitwall.cli_output import json_mode as _json_mode
from pitwall.runpod_client import templates
from pitwall.runpod_client.pods import (
    create_pod_with_fallback_sync,
    get_pod_sync,
    terminate_pod_sync,
)
from pitwall.runpod_client.templates import (
    _TEMPLATE_ENV_KEYS,
    ensure_template,
    get_registry_auth_id_from_env,
)

_PROVIDER_HEALTH_STATUSES = ("unknown", "healthy", "unhealthy", "hibernated")
_DEFAULT_INIT_SEED_PATH = "seed"
_DEFAULT_INIT_CAPABILITY_NAME = "embedding.demo"
_DEFAULT_INIT_PROVIDER_NAME = "demo-runpod-lb"
_DEFAULT_INIT_ENDPOINT_ID = "eptest00000000"
_DEFAULT_INIT_PROVIDER_TYPE = "serverless_lb"
_DEFAULT_INIT_REGION = "US-EXAMPLE-1"
_DEFAULT_INIT_GPU_CLASS = "NVIDIA L4"
_DEFAULT_INIT_PER_SECOND_ACTIVE = "0.001"
_DEFAULT_INIT_SMOKE_BASE_URL = "http://127.0.0.1:8080"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        _usage(stream=sys.stdout)
        return 0

    if args in (["-h"], ["--help"], ["help"]):
        _usage(stream=sys.stdout)
        return 0

    if args in (["-V"], ["--version"]):
        print(_installed_version())
        return 0

    group = args[0]
    rest = args[1:]

    if group == "db":
        from pitwall.db import main as db_main

        return db_main(rest)

    if group == "register-template":
        return cmd_register_template(rest)

    if group == "init":
        return cmd_init(rest)

    if group == "create-capability":
        return cmd_create_capability(rest)

    if group == "seed":
        return cmd_seed(rest)

    if group == "config":
        return cmd_config(rest)

    if group == "terminate-pod":
        return cmd_terminate_pod(rest)

    if group == "register-endpoint":
        return cmd_register_endpoint(rest)

    if group == "set-provider-health":
        return cmd_set_provider_health(rest)

    if group == "warm-volume":
        return cmd_warm_volume(rest)

    if group == "dashboard":
        return cmd_dashboard(rest)

    if group == "mcp":
        return cmd_mcp_serve(rest)

    if group == "retention":
        from pitwall.retention.__main__ import main as retention_main

        return retention_main(rest)

    print(f"Unknown command group: {group}", file=sys.stderr)
    _usage(stream=sys.stderr)
    return 1


def _installed_version() -> str:
    """Return the installed distribution version with a source-tree fallback."""
    try:
        return version("pitwall-gpu-broker")
    except PackageNotFoundError:
        from pitwall import __version__

        return __version__


def _usage(*, stream: TextIO) -> None:
    print(
        "Usage: pitwall-gpu-broker {db|mcp|retention|init|create-capability|seed|config|register-template|register-endpoint|set-provider-health|terminate-pod|warm-volume|dashboard} <command>",
        file=stream,
    )
    print("  pitwall-gpu-broker --help              Show this help and exit", file=stream)
    print(
        "  pitwall-gpu-broker --version           Show the installed version and exit", file=stream
    )
    print("  pitwall-gpu-broker db migrate          Apply pending migrations", file=stream)
    print("  pitwall-gpu-broker db reset            Drop the pitwall schema", file=stream)
    print("  pitwall-gpu-broker db status           Show migration status", file=stream)
    print(
        "  pitwall-gpu-broker mcp serve           Start the MCP server",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker retention run       Encrypted bounded archive/purge",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker init                Guided local onboarding",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker create-capability   Create or update a capability",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker seed                Apply capability/provider seed files",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker config check        Validate boot-time configuration",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker register-template   Register a RunPod template",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker register-endpoint   Register a RunPod endpoint as a provider",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker set-provider-health  Mark a provider healthy/unhealthy/hibernated",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker terminate-pod        Terminate a RunPod pod by id",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker warm-volume          Pre-warm a volume with capability-specific assets",
        file=stream,
    )
    print(
        "  pitwall-gpu-broker dashboard            Launch the Textual operator console",
        file=stream,
    )


def cmd_dashboard(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker dashboard",
        description="Launch the Textual operator console.",
    )
    parser.parse_args(argv)

    from pitwall.tui import PitwallApp

    PitwallApp().run()
    return 0


def _parse_register_template_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker register-template",
        description="Register a RunPod template and cache its ID.",
    )
    parser.add_argument(
        "--image",
        required=True,
        help="Full image reference (e.g., ghcr.io/org/worker:v1 or ghcr.io/org/worker@sha256:...)",
    )
    parser.add_argument(
        "--template-name",
        default="pitwall-cloud-worker",
        help="Base template name (default: pitwall-cloud-worker)",
    )
    parser.add_argument(
        "--container-disk-gb",
        type=int,
        default=50,
        help="Container disk size in GB (default: 50)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate image parsing, registry auth selection, template defaults, and env schema without network calls.",
    )
    add_json_argument(parser)
    return parser.parse_args(argv)


async def _register_template_async(args: argparse.Namespace) -> int:
    from pitwall.db import get_pool

    out = Output(_json_mode(args))
    image_ref = args.image
    template_name = args.template_name
    container_disk_gb = args.container_disk_gb

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        out.print_error("RUNPOD_API_KEY environment variable is not set")
        out.emit()
        return 1

    registry_auth_id = get_registry_auth_id_from_env(image_ref)

    pool = await get_pool()

    template_id = await ensure_template(
        pool,
        image_ref,
        template_name=template_name,
        registry_auth_id=registry_auth_id,
        container_disk_gb=container_disk_gb,
    )
    out.print_success(f"Template registered: {template_id}")
    out.add_json("template_id", template_id)
    out.emit()
    return 0


def _dry_run_validate(args: argparse.Namespace) -> int:
    out = Output(_json_mode(args))
    image_ref = args.image
    template_name = args.template_name
    container_disk_gb = args.container_disk_gb

    image_parsed = templates.image_sha(image_ref)
    normalized_name = templates.normalize_template_name(template_name)
    display_name = templates.template_display_name(template_name, image_ref)
    registry_auth_id = get_registry_auth_id_from_env(image_ref)

    lines = [
        f"[dry-run] image: {image_ref}",
        f"[dry-run] image_sha: {image_parsed}",
        f"[dry-run] template_name: {template_name} -> {normalized_name}",
        f"[dry-run] template_display_name: {display_name}",
        f"[dry-run] container_disk_gb: {container_disk_gb}",
        f"[dry-run] registry_auth_id: {registry_auth_id}",
        f"[dry-run] env_keys ({len(_TEMPLATE_ENV_KEYS)}): {_TEMPLATE_ENV_KEYS}",
    ]
    out.print_panel("\n".join(lines), title="Dry Run", border_style="blue")
    out.add_json("image", image_ref)
    out.add_json("image_sha", image_parsed)
    out.add_json("template_name", template_name)
    out.add_json("normalized_name", normalized_name)
    out.add_json("template_display_name", display_name)
    out.add_json("container_disk_gb", container_disk_gb)
    out.add_json("registry_auth_id", registry_auth_id)
    out.add_json("env_keys", list(_TEMPLATE_ENV_KEYS))
    out.emit()
    return 0


def cmd_register_template(argv: list[str]) -> int:
    args = _parse_register_template_args(argv)

    if args.dry_run:
        return _dry_run_validate(args)

    import asyncio

    out = Output(_json_mode(args))
    try:
        return asyncio.run(_register_template_async(args))
    except (
        Exception
    ) as e:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(f"Error: {e}")
        out.emit()
        return 1


def _enum_values(enum_type: type[Any]) -> list[str]:
    return [item.value for item in enum_type]


def _parse_create_capability_args(argv: list[str]) -> argparse.Namespace:
    from pitwall.core.enums import CapabilityClass, CapabilityHint, CostMode

    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker create-capability",
        description="Create or update a Pitwall capability by flags or spec file.",
    )
    parser.add_argument(
        "--spec",
        help="YAML/JSON capability spec file. May contain one capability or a capabilities list.",
    )
    parser.add_argument("--name", help="Capability name, e.g. embedding.demo")
    parser.add_argument("--version", default="1.0.0", help="Capability version (default: 1.0.0)")
    parser.add_argument(
        "--class",
        dest="capability_class",
        choices=_enum_values(CapabilityClass),
        help="Capability class",
    )
    parser.add_argument(
        "--cost-mode",
        choices=_enum_values(CostMode),
        help="Cost estimator mode",
    )
    parser.add_argument("--description", help="Human-readable capability description")
    parser.add_argument(
        "--hint",
        dest="hints",
        action="append",
        choices=_enum_values(CapabilityHint),
        default=[],
        help="Capability hint. May be repeated.",
    )
    parser.add_argument(
        "--openai-compatible",
        action="store_true",
        help="Mark the capability as OpenAI-compatible in registry config.",
    )
    add_json_argument(parser)
    return parser.parse_args(argv)


async def _create_capability_async(args: argparse.Namespace) -> int:
    from pitwall.core.enums import CapabilitySource
    from pitwall.db import get_pool
    from pitwall.db.repository import CapabilityRepository
    from pitwall.seed import SeedValidationError, apply_capability_seed_files

    out = Output(_json_mode(args))

    if args.spec:
        if args.name or args.capability_class or args.cost_mode:
            out.print_error("use either --spec or --name/--class/--cost-mode flags")
            out.emit()
            return 1
        try:
            pool = await get_pool()
            capabilities = await apply_capability_seed_files(
                [args.spec],
                pool=pool,
                source=CapabilitySource.API,
            )
        except SeedValidationError as exc:
            out.print_error(str(exc))
            out.emit()
            return 1
        if not capabilities:
            out.print_error(f"no capabilities found in spec: {args.spec}")
            out.emit()
            return 1
        out.add_json("capabilities", [_safe_json(c) for c in capabilities])
        for capability in capabilities:
            _print_capability_created(capability, out)
        out.emit()
        return 0

    missing = []
    if args.name is None:
        missing.append("--name")
    if args.capability_class is None:
        missing.append("--class")
    if args.cost_mode is None:
        missing.append("--cost-mode")
    if missing:
        out.print_error(f"missing required arguments: {', '.join(missing)}")
        out.emit()
        return 1

    name = args.name.strip()
    if not name:
        out.print_error("capability name cannot be empty")
        out.emit()
        return 1

    pool = await get_pool()
    repo = CapabilityRepository(pool)
    capability = await repo.upsert(
        name=name,
        class_=args.capability_class,
        cost_mode=args.cost_mode,
        version=args.version,
        description=args.description,
        hints_supported=args.hints,
        openai_compatible=args.openai_compatible,
    )
    out.add_json("capability", _safe_json(capability))
    _print_capability_created(capability, out)
    out.emit()
    return 0


def _print_capability_created(capability: Any, out: Output) -> None:
    out.print_success(
        f"Capability created: {capability.id}\n"
        f"  name: {capability.name}\n"
        f"  class: {capability.class_.value}\n"
        f"  cost_mode: {capability.cost_mode.value}"
    )


def cmd_create_capability(argv: list[str]) -> int:
    args = _parse_create_capability_args(argv)

    import asyncio

    out = Output(_json_mode(args))
    try:
        return asyncio.run(_create_capability_async(args))
    except (
        Exception
    ) as e:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(f"Error: {e}")
        out.emit()
        return 1


def _parse_seed_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker seed",
        description="Apply Pitwall capability/provider seed files.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="YAML/JSON seed file or directory containing seed files.",
    )
    parser.add_argument(
        "--mark-healthy",
        action="store_true",
        help="After applying providers, mark them healthy using the provider health path.",
    )
    add_json_argument(parser)
    return parser.parse_args(argv)


async def _seed_async(args: argparse.Namespace) -> int:
    from pitwall.core.enums import CapabilitySource
    from pitwall.db import get_pool
    from pitwall.seed import SeedValidationError, apply_seed_files

    out = Output(_json_mode(args))

    try:
        pool = await get_pool()
        result = await apply_seed_files(args.paths, pool=pool, source=CapabilitySource.YAML)
        if args.mark_healthy:
            for provider in result.providers:
                healthy = await _apply_provider_health(pool, provider.id, "healthy")
                if healthy is None:
                    out.print_error(f"Provider not found: {provider.id}")
                    out.emit()
                    return 1
    except SeedValidationError as exc:
        out.print_error(str(exc))
        out.emit()
        return 1

    cap_rows: list[list[Any]] = []
    for capability in result.capabilities:
        cap_rows.append([capability.id, capability.name])
    prov_rows: list[list[Any]] = []
    for provider in result.providers:
        health = "healthy" if args.mark_healthy else provider.health_status
        prov_rows.append([provider.id, provider.name, health])

    if cap_rows:
        out.print_table("Capabilities", ["id", "name"], cap_rows)
    if prov_rows:
        out.print_table("Providers", ["id", "name", "health"], prov_rows)

    out.add_json("capabilities", [_safe_json(c) for c in result.capabilities])
    out.add_json("providers", [_safe_json(p) for p in result.providers])
    out.emit()
    return 0


def cmd_seed(argv: list[str]) -> int:
    args = _parse_seed_args(argv)

    import asyncio

    out = Output(_json_mode(args))
    try:
        return asyncio.run(_seed_async(args))
    except (
        Exception
    ) as e:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(f"Error: {e}")
        out.emit()
        return 1


def _parse_config_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker config",
        description="Inspect and validate Pitwall runtime configuration.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser(
        "check",
        help="Validate boot-time configuration for a service.",
    )
    check.add_argument(
        "service",
        nargs="?",
        default="api",
        help="Service name to validate (default: api).",
    )
    add_json_argument(check)
    return parser.parse_args(argv)


def cmd_config(argv: list[str]) -> int:
    from pydantic import ValidationError

    from pitwall.config import (
        ConfigFileError,
        check_domain_config,
        format_config_check_result,
        format_settings_load_error,
    )

    args = _parse_config_args(argv)
    out = Output(_json_mode(args))
    if args.command != "check":
        return 1
    try:
        result = check_domain_config(args.service)
    except (ConfigFileError, ValidationError, ValueError) as exc:
        err_msg = format_settings_load_error(exc)
        out.print_error(err_msg)
        out.add_json("error", err_msg)
        out.emit()
        return os.EX_CONFIG

    report = format_config_check_result(result)
    if result.errors:
        out.print_error(report)
        out.add_json("errors", [_safe_json(e) for e in result.errors])
        out.emit()
        return os.EX_CONFIG
    out.print_success(report)
    out.add_json("service", result.service)
    out.add_json("status", "ok")
    out.emit()
    return 0


def _parse_init_args(argv: list[str]) -> argparse.Namespace:
    from pitwall.core.enums import CapabilityClass, CostMode, ProviderType

    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker init",
        description="Guided onboarding for a first capability and provider.",
    )
    parser.add_argument(
        "--from-seed",
        help=(
            "Seed file or directory to apply. Defaults to ./seed when it exists; "
            "use --manual to ignore it."
        ),
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Use manual/default values instead of the example seed directory.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt; use supplied flags and documented defaults.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Alias for --non-interactive for scripted setup.",
    )
    parser.add_argument("--capability-name")
    parser.add_argument("--capability-class", choices=_enum_values(CapabilityClass))
    parser.add_argument("--cost-mode", choices=_enum_values(CostMode))
    parser.add_argument("--provider-name")
    parser.add_argument("--endpoint-id")
    parser.add_argument("--provider-type", choices=_enum_values(ProviderType))
    parser.add_argument("--region")
    parser.add_argument("--gpu-class")
    parser.add_argument("--per-second-active")
    parser.add_argument("--priority", type=int, default=1)
    parser.add_argument(
        "--smoke-base-url",
        default=_DEFAULT_INIT_SMOKE_BASE_URL,
        help=f"Base URL used in the printed smoke command (default: {_DEFAULT_INIT_SMOKE_BASE_URL})",
    )
    parser.add_argument(
        "--smoke-text",
        default="hello",
        help="Text used in the printed dry-run inference smoke command.",
    )
    add_json_argument(parser)
    return parser.parse_args(argv)


async def _init_async(args: argparse.Namespace) -> int:
    from pitwall.core.enums import CapabilitySource
    from pitwall.db import get_pool
    from pitwall.seed import SeedValidationError, apply_seed_data, apply_seed_files

    out = Output(_json_mode(args))

    try:
        pool = await get_pool()
        seed_path = _init_seed_path(args)
        if seed_path is not None:
            result = await apply_seed_files([seed_path], pool=pool, source=CapabilitySource.YAML)
        else:
            result = await apply_seed_data(
                _manual_init_seed_payload(args),
                pool=pool,
                source=CapabilitySource.API,
            )
    except SeedValidationError as exc:
        out.print_error(str(exc))
        out.emit()
        return 1

    if not result.capabilities:
        out.print_error("init did not create or update any capability")
        out.emit()
        return 1
    if not result.providers:
        out.print_error("init did not create or update any provider")
        out.emit()
        return 1

    provider = result.providers[0]
    healthy = await _apply_provider_health(pool, provider.id, "healthy")
    if healthy is None:
        out.print_error(f"Provider not found: {provider.id}")
        out.emit()
        return 1

    capability = result.capabilities[0]
    out.print_success(
        f"Pitwall init complete\n"
        f"  capability: {capability.name} ({capability.id})\n"
        f"  provider: {provider.name} ({provider.id})\n"
        f"  health_status: healthy"
    )
    out.add_json("capability", _safe_json(capability))
    out.add_json("provider", provider.model_dump(mode="json"))
    out.add_json("health_status", "healthy")
    if not out.json_mode:
        _print_smoke_inference_command(args.smoke_base_url, capability.name, args.smoke_text)
    out.emit()
    return 0


def _init_seed_path(args: argparse.Namespace) -> str | None:
    if args.manual:
        return None
    from_seed: str | None = args.from_seed
    if from_seed:
        return from_seed
    if os.path.exists(_DEFAULT_INIT_SEED_PATH):
        return _DEFAULT_INIT_SEED_PATH
    return None


def _manual_init_seed_payload(args: argparse.Namespace) -> dict[str, Any]:
    interactive = not (args.non_interactive or args.yes) and sys.stdin.isatty()
    capability_name = _prompt_default(
        "Capability name",
        args.capability_name,
        _DEFAULT_INIT_CAPABILITY_NAME,
        interactive=interactive,
    )
    capability_class = _prompt_default(
        "Capability class",
        args.capability_class,
        "embedding",
        interactive=interactive,
    )
    cost_mode = _prompt_default(
        "Cost mode",
        args.cost_mode,
        "per_second",
        interactive=interactive,
    )
    provider_name = _prompt_default(
        "Provider name",
        args.provider_name,
        _DEFAULT_INIT_PROVIDER_NAME,
        interactive=interactive,
    )
    endpoint_id = _prompt_default(
        "RunPod endpoint id",
        args.endpoint_id,
        _DEFAULT_INIT_ENDPOINT_ID,
        interactive=interactive,
    )
    provider_type = _prompt_default(
        "Provider type",
        args.provider_type,
        _DEFAULT_INIT_PROVIDER_TYPE,
        interactive=interactive,
    )
    region = _prompt_default(
        "Region",
        args.region,
        _DEFAULT_INIT_REGION,
        interactive=interactive,
    )
    gpu_class = _prompt_default(
        "GPU class",
        args.gpu_class,
        _DEFAULT_INIT_GPU_CLASS,
        interactive=interactive,
    )
    per_second_active = _prompt_default(
        "Cost per active second",
        args.per_second_active,
        _DEFAULT_INIT_PER_SECOND_ACTIVE,
        interactive=interactive,
    )
    return {
        "capabilities": [
            {
                "name": capability_name,
                "version": "1.0.0",
                "class": capability_class,
                "description": "Local onboarding capability",
                "cost_mode": cost_mode,
            }
        ],
        "providers": [
            {
                "name": provider_name,
                "capability": capability_name,
                "endpoint_id": endpoint_id,
                "provider_type": provider_type,
                "region": region,
                "gpu_class": gpu_class,
                "priority": args.priority,
                "cost": {
                    "mode": cost_mode,
                    "per_second_active": per_second_active,
                },
            }
        ],
    }


def _prompt_default(
    label: str,
    supplied: str | None,
    default: str,
    *,
    interactive: bool,
) -> str:
    if supplied is not None:
        return supplied
    if not interactive:
        return default
    raw = input(f"{label} [{default}]: ").strip()
    return raw or default


def _print_smoke_inference_command(base_url: str, capability_name: str, text: str) -> None:
    payload = {"capability": capability_name, "texts": [text], "dry_run": True}
    endpoint = f"{base_url.rstrip('/')}/v1/inference"
    print("Next smoke command:")
    print(f"curl -s -X POST {endpoint} \\")
    print("  -H 'Content-Type: application/json' \\")
    print(f"  -d '{_json_dumps(payload)}'")


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def cmd_init(argv: list[str]) -> int:
    args = _parse_init_args(argv)

    import asyncio

    out = Output(_json_mode(args))
    try:
        return asyncio.run(_init_async(args))
    except (
        Exception
    ) as e:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(f"Error: {e}")
        out.emit()
        return 1


_TERMINATE_VERIFY_TIMEOUT_S = 60.0
_TERMINATE_VERIFY_INTERVAL_S = 5.0


def _parse_terminate_pod_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker terminate-pod",
        description="Terminate a single RunPod pod by id with verification.",
    )
    parser.add_argument(
        "--pod-id",
        required=True,
        help="RunPod pod id to terminate",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip post-terminate verification (NOT recommended)",
    )
    parser.add_argument(
        "--verify-timeout-s",
        type=float,
        default=_TERMINATE_VERIFY_TIMEOUT_S,
        help=f"Seconds to wait for pod to reach EXITED/TERMINATED (default {_TERMINATE_VERIFY_TIMEOUT_S})",
    )
    add_json_argument(parser)
    return parser.parse_args(argv)


def _is_terminated(pod: dict[str, Any]) -> bool:
    status = pod.get("desiredStatus", "")
    return status in {"EXITED", "TERMINATED"}


def cmd_terminate_pod(argv: list[str]) -> int:
    args = _parse_terminate_pod_args(argv)
    out = Output(_json_mode(args))

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        out.print_error("RUNPOD_API_KEY env var is required")
        out.emit()
        return 1

    try:
        terminate_pod_sync(args.pod_id)
    except Exception as exc:  # reason: CLI boundary: report terminate failure and exit nonzero
        out.print_error(f"terminate_pod({args.pod_id!r}) raised: {exc!r}")
        out.print_warning("Manual teardown via RunPod console required.")
        out.emit()
        return 1

    out.print(f"terminate_pod called for {args.pod_id}")
    out.add_json("pod_id", args.pod_id)
    out.add_json("action", "terminate_called")

    if args.no_verify:
        out.emit()
        return 0

    deadline = time.monotonic() + args.verify_timeout_s
    while time.monotonic() < deadline:
        try:
            pod = get_pod_sync(args.pod_id)
        except (
            Exception
        ) as exc:  # reason: transient RunPod API errors must not abort the verification poll
            out.print_warning(f"get_pod raised during verification: {exc!r}")
            time.sleep(_TERMINATE_VERIFY_INTERVAL_S)
            continue
        if pod is None:
            msg = f"pod {args.pod_id} no longer returned by RunPod"
            out.print_success(f"OK: {msg}")
            out.add_json("status", "gone")
            out.emit()
            return 0
        if _is_terminated(pod):
            msg = f"pod {args.pod_id} desiredStatus={pod.get('desiredStatus')}"
            out.print_success(f"OK: {msg}")
            out.add_json("status", pod.get("desiredStatus"))
            out.emit()
            return 0
        out.print(f"  ... waiting (desiredStatus={pod.get('desiredStatus')!r})")
        time.sleep(_TERMINATE_VERIFY_INTERVAL_S)

    out.print(
        f"WARN: pod {args.pod_id} did not reach EXITED/TERMINATED within {args.verify_timeout_s}s"
    )
    out.print_warning("Manual verification via RunPod console required.")
    out.add_json("status", "timeout")
    out.emit()
    return 1


def _parse_register_endpoint_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker register-endpoint",
        description="Register a RunPod Serverless endpoint as a Pitwall provider.",
    )
    parser.add_argument(
        "--endpoint-id",
        required=True,
        help="RunPod endpoint ID (e.g., eptest00000000)",
    )
    parser.add_argument(
        "--provider-type",
        required=True,
        choices=["serverless_queue", "serverless_lb", "public_endpoint", "pod_lease"],
        help="RunPod provider surface type",
    )
    parser.add_argument(
        "--capability-id",
        required=True,
        help="Capability ID this endpoint fulfills (e.g., cap_llm_qwen3_32b)",
    )
    parser.add_argument(
        "--capability-name",
        help="Capability human-readable name (e.g., llm.qwen3-32b). If the capability does not exist, it will be upserted with this name.",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Human-readable provider name",
    )
    parser.add_argument(
        "--region",
        help="RunPod region ID (e.g., US-KS-2)",
    )
    parser.add_argument(
        "--gpu-class",
        required=True,
        help="Canonical RunPod GPU type (e.g., NVIDIA H100 80GB HBM3)",
    )
    parser.add_argument(
        "--cost-mode",
        choices=["per_second", "per_request", "per_token"],
        help="Cost estimation mode",
    )
    parser.add_argument(
        "--per-second-active",
        type=float,
        help="Cost per active container-second (USD)",
    )
    parser.add_argument(
        "--per-request",
        type=float,
        help="Flat cost per request (USD)",
    )
    parser.add_argument(
        "--per-million-input-tokens",
        type=float,
        help="Cost per million input tokens (USD)",
    )
    parser.add_argument(
        "--per-million-output-tokens",
        type=float,
        help="Cost per million output tokens (USD)",
    )
    parser.add_argument(
        "--workers-min",
        type=int,
        default=0,
        help="Minimum always-on worker count (default: 0)",
    )
    parser.add_argument(
        "--workers-max",
        type=int,
        help="Maximum worker count for auto-scaling",
    )
    parser.add_argument(
        "--idle-timeout-minutes",
        type=int,
        default=0,
        help="Idle timeout before scale-to-zero in minutes (default: 0)",
    )
    parser.add_argument(
        "--flash-boot-verified",
        action="store_true",
        help="FlashBoot has been verified in RunPod console",
    )
    parser.add_argument(
        "--max-payload-mb",
        type=int,
        default=30,
        help="Maximum request payload size in MB (default: 30)",
    )
    parser.add_argument(
        "--request-timeout-s",
        type=int,
        default=330,
        help="Request timeout in seconds (default: 330)",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=0,
        help="Routing priority (lower = preferred, default: 0)",
    )
    parser.add_argument(
        "--health",
        choices=_PROVIDER_HEALTH_STATUSES,
        default="unknown",
        help="Initial provider health status (default: unknown). Use healthy to make the provider immediately routable.",
    )
    add_json_argument(parser)
    return parser.parse_args(argv)


def _missing_capability_message(capability_id: str) -> str:
    return (
        f"ERROR: capability '{capability_id}' does not exist; create it first "
        "with POST /v1/admin/capabilities or pass --capability-name to "
        "register-endpoint to create it by name."
    )


async def _register_endpoint_async(args: argparse.Namespace) -> int:
    import datetime as dt

    from pitwall.core.enums import CapabilitySource, CostMode, ProviderType
    from pitwall.core.ids import ulid_new
    from pitwall.core.models import Provider
    from pitwall.db import get_pool
    from pitwall.db.repository import CapabilityRepository, ProviderRepository
    from pitwall.runpod_client.gpu import validate_canonical_gpu_name

    out = Output(_json_mode(args))

    pool = await get_pool()
    cap_repo = CapabilityRepository(pool)
    prov_repo = ProviderRepository(pool)

    endpoint_id = args.endpoint_id.strip()
    provider_type = ProviderType(args.provider_type)
    capability_id = args.capability_id.strip()
    capability_name = args.capability_name.strip() if args.capability_name else None
    name = args.name.strip()
    region = args.region.strip() if args.region else None
    gpu_class = validate_canonical_gpu_name(args.gpu_class)

    cost_config: dict[str, Any] = {}
    if args.cost_mode:
        cost_config["mode"] = args.cost_mode
    if args.per_second_active is not None:
        cost_config["per_second_active"] = args.per_second_active
    if args.per_request is not None:
        cost_config["per_request"] = args.per_request
    if args.per_million_input_tokens is not None:
        cost_config["per_million_input_tokens"] = args.per_million_input_tokens
    if args.per_million_output_tokens is not None:
        cost_config["per_million_output_tokens"] = args.per_million_output_tokens

    workers_config: dict[str, Any] = {"workers_min": args.workers_min}
    if args.workers_max is not None:
        workers_config["workers_max"] = args.workers_max

    config: dict[str, Any] = {
        "gpu_class": gpu_class,
        "cost": cost_config,
        "workers": workers_config,
        "idle_timeout_minutes": args.idle_timeout_minutes,
        "flash_boot_verified": args.flash_boot_verified,
        "max_payload_mb": args.max_payload_mb,
        "request_timeout_s": args.request_timeout_s,
    }

    if provider_type == ProviderType.SERVERLESS_LB:
        config["lb_base_url"] = f"https://{endpoint_id}.api.runpod.ai"
    elif provider_type in (ProviderType.SERVERLESS_QUEUE, ProviderType.PUBLIC_ENDPOINT):
        config["openai_base_url"] = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1"

    existing = await prov_repo.get_by_name(name)
    if existing is not None:
        out.print_error(f"Provider with name {name!r} already exists (id={existing.id})")
        out.emit()
        return 1

    if capability_name:
        cost_mode_str = args.cost_mode or "per_second"
        try:
            cost_mode = CostMode(cost_mode_str)
        except ValueError:
            cost_mode = CostMode.PER_SECOND
        capability = await cap_repo.upsert(
            name=capability_name,
            class_="llm",
            cost_mode=cost_mode.value,
            openai_compatible=True,
        )
        capability_id = capability.id
    else:
        existing_capability = await cap_repo.get(capability_id)
        if existing_capability is None:
            out.print_error(_missing_capability_message(capability_id))
            out.emit()
            return 1

    now = dt.datetime.now(dt.UTC)
    prov_id = f"prov_{ulid_new()}"
    prov = Provider(
        id=prov_id,
        capability_id=capability_id,
        name=name,
        provider_type=provider_type,
        runpod_endpoint_id=endpoint_id,
        runpod_template_id=None,
        region=region,
        cloud_type=None,
        config=config,
        priority=args.priority,
        enabled=True,
        health_status=args.health,
        consecutive_failures=0,
        cooldown_trips=0,
        cold_start_p50_ms=None,
        cold_start_p95_ms=None,
        recent_error_rate=0.0,
        cooldown_until=None,
        source=CapabilitySource.API,
        last_applied_yaml_hash=None,
        updated_at=now,
    )

    result = await prov_repo.create(prov)
    out.print_success(
        f"Provider registered: {result.id}\n"
        f"  name: {result.name}\n"
        f"  capability_id: {result.capability_id}\n"
        f"  provider_type: {result.provider_type.value}\n"
        f"  runpod_endpoint_id: {result.runpod_endpoint_id}\n"
        f"  region: {result.region}\n"
        f"  gpu_class: {config.get('gpu_class')}\n"
        f"  priority: {result.priority}\n"
        f"  health_status: {result.health_status}"
    )
    out.add_json("provider", _safe_json(result))
    out.emit()
    return 0


def cmd_register_endpoint(argv: list[str]) -> int:
    args = _parse_register_endpoint_args(argv)

    import asyncio

    out = Output(_json_mode(args))
    try:
        return asyncio.run(_register_endpoint_async(args))
    except (
        Exception
    ) as e:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(f"Error: {e}")
        out.emit()
        return 1


def _parse_set_provider_health_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker set-provider-health",
        description="Set a provider health status used by routing.",
    )
    parser.add_argument("provider_id", help="Pitwall provider id")
    parser.add_argument("health", choices=_PROVIDER_HEALTH_STATUSES)
    add_json_argument(parser)
    return parser.parse_args(argv)


async def _set_provider_health_async(args: argparse.Namespace) -> int:
    from pitwall.db import get_pool

    out = Output(_json_mode(args))
    provider_id = args.provider_id.strip()
    health_status = args.health.strip()

    pool = await get_pool()
    result = await _apply_provider_health(pool, provider_id, health_status)
    if result is None:
        out.print_error(f"Provider not found: {provider_id}")
        out.emit()
        return 1

    out.print_success(
        f"Provider health updated: {result.id}\n"
        f"  name: {result.name}\n"
        f"  health_status: {result.health_status}"
    )
    out.add_json("provider", _safe_json(result))
    out.emit()
    return 0


def cmd_set_provider_health(argv: list[str]) -> int:
    args = _parse_set_provider_health_args(argv)

    import asyncio

    out = Output(_json_mode(args))
    try:
        return asyncio.run(_set_provider_health_async(args))
    except (
        Exception
    ) as e:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(f"Error: {e}")
        out.emit()
        return 1


async def _apply_provider_health(pool: Any, provider_id: str, health_status: str) -> Any | None:
    from pitwall.db.repository import ProviderRepository

    repo = ProviderRepository(pool)

    existing = await repo.get(provider_id)
    if existing is None:
        return None

    patch: dict[str, Any] = {"health_status": health_status}
    if health_status == "healthy":
        patch.update(
            {
                "consecutive_failures": 0,
                "cooldown_trips": 0,
                "recent_error_rate": 0.0,
                "cooldown_until": None,
            }
        )
    return await repo.patch(provider_id, **patch)


_WARM_VOLUME_DEFAULT_TIMEOUT_S = 1800

_WARM_VOLUME_POD_CREATE_FAILED_SQL = """
    UPDATE pitwall.workloads
    SET state = 'failed',
        completed_at = $2,
        execution_ms = 0,
        cost_actual_usd = $3,
        error = $4::jsonb
    WHERE id = $1
"""


def _parse_warm_volume_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker warm-volume",
        description="Pre-warm a volume with capability-specific assets by booting a pod, running a download script, and terminating.",
    )
    parser.add_argument(
        "--capability",
        required=True,
        help="Capability ID to warm (e.g., cap_llm_qwen3_32b)",
    )
    parser.add_argument(
        "--volume-id",
        required=True,
        help="RunPod network volume ID to warm",
    )
    parser.add_argument(
        "--provider",
        help="Provider ID to use for the warm-up pod (if not specified, uses the best available provider for the capability)",
    )
    parser.add_argument(
        "--script",
        default="default",
        help="Pre-warm script name to run inside the pod (default: default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate parameters and print what would be done without making network calls",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_WARM_VOLUME_DEFAULT_TIMEOUT_S,
        help=f"Maximum seconds to wait for pod to complete (default: {_WARM_VOLUME_DEFAULT_TIMEOUT_S})",
    )
    add_json_argument(parser)
    return parser.parse_args(argv)


def cmd_warm_volume(argv: list[str]) -> int:
    args = _parse_warm_volume_args(argv)
    out = Output(_json_mode(args))

    capability = args.capability
    volume_id = args.volume_id
    provider = args.provider
    script = args.script
    timeout = args.timeout

    if args.dry_run:
        lines = [
            f"[dry-run] capability: {capability}",
            f"[dry-run] volume_id: {volume_id}",
            f"[dry-run] provider: {provider if provider else '(auto-select)'}",
            f"[dry-run] script: {script}",
            f"[dry-run] timeout: {timeout}s",
        ]
        out.print_panel("\n".join(lines), title="Dry Run", border_style="blue")
        out.add_json("capability", capability)
        out.add_json("volume_id", volume_id)
        out.add_json("provider", provider)
        out.add_json("script", script)
        out.add_json("timeout", timeout)
        out.emit()
        return 0

    import asyncio

    try:
        return asyncio.run(_warm_volume_async(args))
    except (
        Exception
    ) as e:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(f"Error: {e}")
        out.emit()
        return 1


def _warm_volume_estimate_capability(capability: Any, timeout_s: int) -> Any:
    defaults = capability.defaults.model_copy(
        update={"execution_timeout_ms": max(1, timeout_s) * 1000}
    )
    return capability.model_copy(update={"defaults": defaults})


async def _close_warm_volume_pod_create_failure(
    pool: Any,
    *,
    workload_id: str,
    error: BaseException,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _WARM_VOLUME_POD_CREATE_FAILED_SQL,
            workload_id,
            dt.datetime.now(dt.UTC),
            Decimal("0"),
            {
                "phase": "warm_volume_pod_create",
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )


async def _warm_volume_async(args: argparse.Namespace) -> int:
    from pitwall.config import load_settings_from_env
    from pitwall.cost.budget_gate import BudgetGate, BudgetRejected
    from pitwall.cost.sync_gate import estimate_cost
    from pitwall.db import get_pool
    from pitwall.db.repository import CapabilityRepository, ProviderRepository
    from pitwall.runpod_client.gpu import validate_canonical_gpu_name
    from pitwall.runpod_client.workloads import WorkloadConfig

    out = Output(_json_mode(args))

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        out.print_error("RUNPOD_API_KEY is required in environment")
        out.emit()
        return 1

    data_center_id = os.environ.get("RUNPOD_DATA_CENTER_ID")
    if not data_center_id:
        out.print_error("RUNPOD_DATA_CENTER_ID is required in environment")
        out.emit()
        return 1

    image_ref = os.environ.get("PITWALL_CLOUD_WORKER_IMAGE")
    if not image_ref:
        out.print_error("PITWALL_CLOUD_WORKER_IMAGE is required in environment")
        out.emit()
        return 1

    capability = args.capability
    volume_id = args.volume_id
    provider_id = args.provider
    script = args.script
    timeout = args.timeout

    pool = await get_pool()
    cap_repo = CapabilityRepository(pool)
    prov_repo = ProviderRepository(pool)

    cap_record = await cap_repo.get_by_name(capability)
    if cap_record is None:
        cap_record = await cap_repo.get(capability)
    if cap_record is None:
        out.print_error(f"Capability not found: {capability}")
        out.emit()
        return 1

    if provider_id:
        provider_record = await prov_repo.get(provider_id)
        if provider_record is None:
            out.print_error(f"Provider not found: {provider_id}")
            out.emit()
            return 1
    else:
        providers = await prov_repo.list(
            capability_id=cap_record.id,
            enabled_only=True,
            limit=10,
        )
        if not providers:
            out.print_error(f"No enabled providers found for capability {capability}")
            out.emit()
            return 1
        provider_record = min(
            providers,
            key=lambda p: (p.priority, p.name, p.id),
        )

    gpu_class = provider_record.config.get("gpu_class") if provider_record.config else None
    if not gpu_class:
        out.print_error(f"Provider {provider_record.id} has no gpu_class in config")
        out.emit()
        return 1

    gpu_types = [validate_canonical_gpu_name(gpu_class)]

    workload = WorkloadConfig(
        name=f"warm-volume-{capability}",
        capability=capability,
        gpu_types=gpu_types,
        gpu_count=1,
        container_disk_gb=20,
        min_vcpu=4,
        min_memory_gb=16,
        cloud_type="ALL",
    )

    warmup_py = _build_prewarm_script(script, capability)
    b64 = base64.b64encode(warmup_py.encode("utf-8")).decode("ascii")
    docker_start_cmd = (
        'printf "%s" "$PREWARM_SCRIPT_B64" | base64 -d > /tmp/prewarm.py && python3 /tmp/prewarm.py'
    )

    env_block: dict[str, str] = {
        "PREWARM_SCRIPT_B64": b64,
    }
    if hf_token := os.environ.get("HUGGINGFACE_TOKEN"):
        env_block["HUGGINGFACE_TOKEN"] = hf_token

    estimate_capability = _warm_volume_estimate_capability(cap_record, timeout)
    estimate_usd = estimate_cost(
        capability=estimate_capability,
        provider_cost=provider_record.config,
        payload={
            "operation": "warm_volume",
            "script": script,
            "timeout_s": timeout,
            "volume_id": volume_id,
        },
    )
    settings = load_settings_from_env()
    budget_gate = BudgetGate(
        pool,
        monthly_budget_usd=settings.pitwall_monthly_budget_usd,
        per_request_max_usd=settings.pitwall_per_request_max_usd,
    )
    try:
        admission = await budget_gate.try_launch_admission(
            capability_id=cap_record.id,
            provider_id=provider_record.id,
            estimate_usd=estimate_usd,
            workload_type="warm_volume",
        )
    except BudgetRejected as exc:
        out.print_error(f"budget rejected: {exc.reason}")
        out.add_json("error", exc.error_code)
        out.add_json("reason", exc.reason)
        out.add_json("snapshot", exc.snapshot.to_serializable_dict())
        out.emit()
        return 2
    workload_id = admission.workload_id
    out.add_json("workload_id", workload_id)

    pod_name = f"pitwall-warm-{capability[:16]}-{int(time.time())}"

    try:
        pod = create_pod_with_fallback_sync(
            name=pod_name,
            template_id=None,
            image_name=image_ref,
            workload=workload,
            env=env_block,
            network_volume_id=volume_id,
            data_center_id=data_center_id,
            docker_entrypoint=["/bin/sh", "-lc"],
            docker_start_cmd=[docker_start_cmd],
            support_public_ip=False,
            startup_timeout_s=float(timeout),
            startup_poll_s=15.0,
        )
    except Exception as fail:  # reason: any pod-create failure must close the admitted workload to release the reservation
        await _close_warm_volume_pod_create_failure(
            pool,
            workload_id=workload_id,
            error=fail,
        )
        out.print_error(f"all GPU types failed; last error: {fail}")
        out.emit()
        return 2

    pod_id = pod["id"]
    out.print(f"polling pod {pod_id}")
    out.add_json("pod_id", pod_id)

    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        try:
            p = get_pod_sync(pod_id)
            if p is None or (isinstance(p, dict) and not p):
                out.print("pod no longer in inventory — RunPod removed it; assuming clean exit")
                out.add_json("status", "gone")
                break
            desired = p.get("desiredStatus") if isinstance(p, dict) else None
            runtime = p.get("runtime") if isinstance(p, dict) else None
            runtime_status = None
            if isinstance(runtime, dict):
                runtime_status = runtime.get("status") or (runtime.get("container") or {}).get(
                    "status"
                )
            if desired != last_status:
                out.print(f"pod state: desired={desired} runtime={runtime_status}")
                last_status = desired
            if runtime_status and runtime_status.upper() in {"EXITED", "TERMINATED", "STOPPED"}:
                out.print("pod exited cleanly")
                out.add_json("status", "exited")
                break
            if desired == "EXITED":
                out.print("pod desiredStatus=EXITED")
                out.add_json("status", "exited")
                break
        except Exception as exc:  # reason: transient poll errors tolerated until timeout
            out.print(f"  poll error: {exc}")
        time.sleep(15)
    else:
        out.print_warning("timeout waiting for pod exit")
        out.add_json("status", "timeout")

    try:
        terminate_pod_sync(pod_id)
        out.print(f"terminated pod {pod_id}")
        out.add_json("terminated", True)
    except (
        Exception
    ) as exc:  # reason: best-effort cleanup: report terminate failure, keep pod status output
        out.print_error(f"terminate failed: {exc}")
        out.add_json("terminated", False)
        out.emit()
        return 3

    out.emit()
    return 0


def _build_prewarm_script(script_name: str, capability: str) -> str:
    if script_name == "default":
        return (
            "import os\n"
            "import sys\n"
            "print('PREWARM_START', flush=True)\n"
            "print('capability=', sys.argv[1] if len(sys.argv) > 1 else os.environ.get('CAPABILITY', 'unknown'), flush=True)\n"
            "print('PREWARM_COMPLETE', flush=True)\n"
        )
    return (
        "import os\n"
        "import sys\n"
        "print('PREWARM_START', flush=True)\n"
        f"print('script={script_name}', flush=True)\n"
        "print('PREWARM_COMPLETE', flush=True)\n"
    )


def _parse_mcp_serve_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitwall-gpu-broker mcp serve",
        description="Start the Pitwall MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default=os.environ.get("PITWALL_MCP_TRANSPORT", "stdio"),
        help="MCP transport type (public alpha: stdio only)",
    )
    add_json_argument(parser)
    args = parser.parse_args(argv)
    if args.transport != "stdio":
        parser.error("network MCP transports are unavailable in the public alpha")
    return args


def cmd_mcp_serve(argv: list[str]) -> int:
    from pitwall.mcp import ensure_runtime_env, mcp

    args = _parse_mcp_serve_args(argv)
    out = Output(_json_mode(args))
    transport = args.transport
    ensure_runtime_env()
    out.add_json("transport", transport)
    out.emit()
    mcp.run(transport=transport)
    return 0
