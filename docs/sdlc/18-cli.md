# CLI Subsystem

## 1. Purpose & Scope

The CLI subsystem is the top-level command-line interface for the Pitwall application. It is invoked as `pitwall-gpu-broker` after installation or as `python -m pitwall` from the import package.

The command groups covered in this chapter are:

| Group | Responsibility |
|---|---|
| `db` | Database lifecycle (migrations, schema reset, status) |
| `retention` | Encrypted bounded archive and purge lifecycle |
| `init` | Guided local onboarding for a first capability and provider (`src/pitwall/cli.py:68`, `src/pitwall/cli.py:468`) |
| `create-capability` | Create or update capabilities by flags or YAML/JSON spec (`src/pitwall/cli.py:71`, `src/pitwall/cli.py:245`) |
| `seed` | Apply capability/provider seed files or directories (`src/pitwall/cli.py:74`, `src/pitwall/cli.py:363`) |
| `config` | Validate boot-time configuration for a service (`src/pitwall/cli.py:77`, `src/pitwall/cli.py:422`) |
| `register-template` | Register a RunPod template and cache its ID in the DB |
| `terminate-pod` | Terminate a single RunPod pod by ID with optional post-terminate verification |
| `register-endpoint` | Register a RunPod Serverless endpoint as a Pitwall provider |
| `set-provider-health` | Mark a provider `healthy`, `unhealthy`, `unknown`, or `hibernated` for routing |
| `warm-volume` | Pre-warm a RunPod network volume by booting a capability-specific pod |
| `dashboard` | Launch the read-only Textual operator console |
| `mcp serve` | Start the Pitwall MCP server |

The `live.py` module is a separate file (not a CLI command) that provides a boolean gate (`is_live`) and an enforcement guard (`require_live`) used by smoke scripts and CLI tools to prevent accidental live RunPod calls in hermetic or CI environments.

---

## 2. Components

### `src/pitwall/__main__.py`

**Responsibility:** Entry-point for `python -m pitwall`. Calls `cli.main()` and exits with its integer return code.

**Key content:**
```python
from pitwall.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

**Invariant:** Exits with `SystemExit(code)` where `code` is the integer return value of `cli.main`.

---

### `src/pitwall/cli_output.py`

**Responsibility:** Rich-based output layer shared by all CLI commands. Provides table, panel, and JSON renderers with a unified API so that commands do not emit bare `print()` calls.

#### `Output` class

```python
class Output:
    def __init__(self, json_mode: bool = False) -> None: ...
```

- `json_mode=False` (default): renders human-friendly tables and panels via `rich.console.Console`.
- `json_mode=True`: suppresses all Rich rendering and accumulates structured data; `emit()` writes a single JSON object to stdout.

Key methods:

| Method | JSON mode | Rich mode |
|---|---|---|
| `print(msg)` | no-op | plain stdout line |
| `print_table(title, columns, rows)` | records list of dicts | `rich.table.Table` |
| `print_panel(content, title, border_style)` | records panel metadata | `rich.panel.Panel` |
| `print_error(message)` | sets `"error"` key | red error panel to stderr |
| `print_warning(message)` | appends to `"warnings"` list | yellow warning panel to stderr |
| `print_success(message)` | sets `"success"` key | green success panel to stdout |
| `add_json(key, value)` | adds key to result dict | no-op |
| `set_json(data)` | replaces result dict | no-op |
| `emit()` | dumps JSON to stdout | no-op |

All commands instantiate `Output(json_mode=getattr(args, "json", False))` at the top of their handler so that `--json` is supported uniformly.

---

### `src/pitwall/tui/`

**Responsibility:** Textual-based operator console. It currently ships read-only Overview and Providers screens. Overview summarizes provider counts, provider health, lease states, active lease count, recent workload count, and persisted cost totals. Providers lists the registered provider plugins by id, status, and pricing-model contract. The TUI uses the same broker state layers and provider registry as the non-interactive surfaces instead of making live provider calls.

Key modules:

| Module | Responsibility |
|---|---|
| `tui.app` | `PitwallApp`, the Textual shell with header/footer, persistent navigation, `q` / `Ctrl-C` quit, `o` Overview, `p` Providers, and lazy DB-pool resolution |
| `tui.overview` | `OverviewScreen`, `OverviewSnapshot`, `OverviewSource`, `PostgresOverviewSource`, `StaticOverviewSource`, status/count formatting helpers |
| `tui.providers` | `ProvidersScreen`, `ProvidersSnapshot`, `ProvidersSource`, `RegistryProvidersSource`, `StaticProvidersSource`, fixed-width provider table formatting |
| `tui.confirmation` | Typed confirmation-tier stubs for future mutating screens; the current Overview has no destructive action widgets |

`PostgresOverviewSource` reads providers via `ProviderRepository.list`, lease state counts from `pitwall.leases`, cost totals via `pitwall.core.cost_reporting.cost_summary`, and recent workload counts via `pitwall.core.cost_reporting.recent_workloads`. Tests inject `StaticOverviewSource`, so Textual Pilot coverage is hermetic and never opens a DB connection.

`RegistryProvidersSource` enumerates `pitwall.providers.registry.get_default_registry()` read-only. It renders the registry ids in registration order with `registered` status and a `tagged` pricing-model label, so future provider plugins become visible when they are registered in the default registry. Tests inject `StaticProvidersSource`, so Pilot coverage never imports live credentials or calls provider APIs.

---

### `src/pitwall/cli.py`

**Responsibility:** Top-level dispatcher and all subcommand implementations. Uses `argparse` for flag parsing. All functions return an integer exit code (0 = success, non-zero = failure). Every parser includes `--json` via `add_json_argument(parser)`.

#### Module-level constants

| Constant | Value | Meaning |
|---|---|---|
| `_TERMINATE_VERIFY_TIMEOUT_S` | `60.0` | Default max seconds to wait for pod to reach `EXITED`/`TERMINATED` after terminate call |
| `_TERMINATE_VERIFY_INTERVAL_S` | `5.0` | Polling interval (seconds) during terminate verification loop |
| `_WARM_VOLUME_DEFAULT_TIMEOUT_S` | `1800` | Default max seconds to wait for warm-up pod to complete |
| `_PROVIDER_HEALTH_STATUSES` | `("unknown", "healthy", "unhealthy", "hibernated")` | CLI-accepted provider health values |

#### Dispatcher: `main`

```python
def main(argv: list[str] | None = None) -> int
```

- `argv`: Command-line args (excluding the program name). Defaults to `sys.argv[1:]`.
- Reads `args[0]` as the command group, delegates to the appropriate `cmd_*` function.
- Prints usage to stderr and returns `1` when called with no args or an unknown group.

Supported groups covered here: `db`, `init`, `create-capability`, `seed`, `config`, `register-template`, `terminate-pod`, `register-endpoint`, `set-provider-health`, `warm-volume`, `dashboard`, `mcp`.

#### `cmd_register_template` / `_register_template_async` / `_dry_run_validate`

```python
def cmd_register_template(argv: list[str]) -> int
async def _register_template_async(args: argparse.Namespace) -> int
def _dry_run_validate(args: argparse.Namespace) -> int
```

- **`cmd_register_template`**: Parses args; dispatches to `_dry_run_validate` (if `--dry-run`) or wraps `_register_template_async` in `asyncio.run`.
- **`_dry_run_validate`**: Performs no I/O. Prints parsed image SHA, normalized template name, display name, container disk size, registry auth ID, and the known `_TEMPLATE_ENV_KEYS` tuple.
- **`_register_template_async`**: Requires `RUNPOD_API_KEY` env var. Calls `ensure_template(pool, image_ref, ...)` then prints `Template registered: <id>`.

Argument parser: `_parse_register_template_args`

| Flag | Required | Default | Type |
|---|---|---|---|
| `--image` | Yes | — | `str` |
| `--template-name` | No | `"pitwall-cloud-worker"` | `str` |
| `--container-disk-gb` | No | `50` | `int` |
| `--dry-run` | No | `False` | `bool` (store_true) |

#### `cmd_init` / `_init_async`

```python
def cmd_init(argv: list[str]) -> int
async def _init_async(args: argparse.Namespace) -> int
```

- **`cmd_init`**: Parses args and wraps `_init_async` in `asyncio.run`.
- **`_init_async`**: Uses `--from-seed`, the default `./seed` directory when present, or a manual in-memory seed payload. Applies capability/provider seed data, requires at least one capability and one provider, marks the first provider `healthy`, then prints `Pitwall init complete` plus a dry-run `/v1/inference` smoke `curl` (`src/pitwall/cli.py:520`, `src/pitwall/cli.py:562`, `src/pitwall/cli.py:572`, `src/pitwall/cli.py:540`, `src/pitwall/cli.py:547`, `src/pitwall/cli.py:553`, `src/pitwall/cli.py:671`).

Argument parser: `_parse_init_args` (`src/pitwall/cli.py:468`, `src/pitwall/cli.py:517`). Manual-path defaults come from `_DEFAULT_INIT_*` constants and `_manual_init_seed_payload` (`src/pitwall/cli.py:40`, `src/pitwall/cli.py:41`, `src/pitwall/cli.py:42`, `src/pitwall/cli.py:43`, `src/pitwall/cli.py:44`, `src/pitwall/cli.py:45`, `src/pitwall/cli.py:46`, `src/pitwall/cli.py:47`, `src/pitwall/cli.py:48`, `src/pitwall/cli.py:572`). Enum choices come from `CapabilityClass`, `CostMode`, and `ProviderType` (`src/pitwall/core/enums.py:34`, `src/pitwall/core/enums.py:46`, `src/pitwall/core/enums.py:88`).

| Flag | Required | Default | Type |
|---|---|---|---|
| `--from-seed` | No | `None` | seed file or directory |
| `--manual` | No | `False` | `bool` (store_true) |
| `--non-interactive` | No | `False` | `bool` (store_true) |
| `--yes` | No | `False` | `bool` (store_true; alias for `--non-interactive`) |
| `--capability-name` | No | `"embedding.demo"` on manual path | `str` |
| `--capability-class` | No | `"embedding"` on manual path | choice: `embedding`, `rerank`, `llm`, `vision`, `transcribe`, `gpu_lease`, `custom` |
| `--cost-mode` | No | `"per_second"` on manual path | choice: `per_second`, `per_request`, `per_token` |
| `--provider-name` | No | `"demo-runpod-lb"` on manual path | `str` |
| `--endpoint-id` | No | `"eptest00000000"` on manual path | `str` |
| `--provider-type` | No | `"serverless_lb"` on manual path | choice: `serverless_queue`, `serverless_lb`, `public_endpoint`, `pod_lease` |
| `--region` | No | `"US-EXAMPLE-1"` on manual path | `str` |
| `--gpu-class` | No | `"NVIDIA L4"` on manual path | `str` |
| `--per-second-active` | No | `"0.001"` on manual path | `str` |
| `--priority` | No | `1` | `int` |
| `--smoke-base-url` | No | `"http://127.0.0.1:8080"` | `str` |
| `--smoke-text` | No | `"hello"` | `str` |

Seed inputs: `--from-seed` accepts a YAML/JSON file or directory; without `--manual`, `./seed` is used when it exists (`src/pitwall/cli.py:562`, `src/pitwall/cli.py:567`, `src/pitwall/seed.py:67`). Seed outputs are capability/provider registry writes plus a `SeedApplyResult` countable as `capabilities` and `providers` (`src/pitwall/seed.py:53`, `src/pitwall/seed.py:131`, `src/pitwall/seed.py:160`).

#### `cmd_create_capability` / `_create_capability_async`

```python
def cmd_create_capability(argv: list[str]) -> int
async def _create_capability_async(args: argparse.Namespace) -> int
```

- **`cmd_create_capability`**: Parses args and wraps `_create_capability_async` in `asyncio.run`.
- **`_create_capability_async`**: Uses either `--spec` or the manual flag set. `--spec` applies capability entries only from a YAML/JSON spec file; manual mode requires `--name`, `--class`, and `--cost-mode`, then upserts through `CapabilityRepository`. Success prints `Capability created: <id>`, name, class, and cost mode (`src/pitwall/cli.py:286`, `src/pitwall/cli.py:292`, `src/pitwall/cli.py:313`, `src/pitwall/cli.py:329`, `src/pitwall/cli.py:344`).

Argument parser: `_parse_create_capability_args` (`src/pitwall/cli.py:245`, `src/pitwall/cli.py:283`). Enum choices come from `CapabilityClass`, `CostMode`, and `CapabilityHint` (`src/pitwall/core/enums.py:34`, `src/pitwall/core/enums.py:46`, `src/pitwall/core/enums.py:69`).

| Flag | Required | Default | Type |
|---|---|---|---|
| `--spec` | Conditional | `None` | YAML/JSON file path |
| `--name` | Conditional | `None` | `str` |
| `--version` | No | `"1.0.0"` | `str` |
| `--class` | Conditional | `None` | choice: `embedding`, `rerank`, `llm`, `vision`, `transcribe`, `gpu_lease`, `custom` |
| `--cost-mode` | Conditional | `None` | choice: `per_second`, `per_request`, `per_token` |
| `--description` | No | `None` | `str` |
| `--hint` | No | `[]` | repeatable choice: `latency_sensitive`, `cost_sensitive`, `region_preference` |
| `--openai-compatible` | No | `False` | `bool` (store_true) |

`--spec` cannot be combined with `--name`, `--class`, or `--cost-mode`; without `--spec`, those three flags are required (`src/pitwall/cli.py:292`, `src/pitwall/cli.py:313`).

#### `cmd_seed` / `_seed_async`

```python
def cmd_seed(argv: list[str]) -> int
async def _seed_async(args: argparse.Namespace) -> int
```

- **`cmd_seed`**: Parses args and wraps `_seed_async` in `asyncio.run`.
- **`_seed_async`**: Applies one or more seed files/directories through `apply_seed_files`, optionally marks every applied provider `healthy`, then prints `Seed applied:` with capability/provider counts (`src/pitwall/cli.py:381`, `src/pitwall/cli.py:388`, `src/pitwall/cli.py:389`, `src/pitwall/cli.py:399`).

Argument parser: `_parse_seed_args` (`src/pitwall/cli.py:363`, `src/pitwall/cli.py:378`).

| Argument / Flag | Required | Default | Type |
|---|---|---|---|
| `paths` | Yes | — | one or more seed files or directories |
| `--mark-healthy` | No | `False` | `bool` (store_true) |

Seed inputs are YAML/YML/JSON files or directories containing those suffixes; directory files are sorted before loading. Seed outputs are capability/provider registry writes, and duplicate provider names with different IDs fail before writing the conflicting provider (`src/pitwall/seed.py:36`, `src/pitwall/seed.py:67`, `src/pitwall/seed.py:96`, `src/pitwall/seed.py:160`, `src/pitwall/seed.py:183`).

#### `cmd_config`

```python
def cmd_config(argv: list[str]) -> int
```

- **`cmd_config`**: Parses args and runs the `check` subcommand synchronously (no `asyncio.run`); a non-`check` subcommand returns `1` (`src/pitwall/cli.py:441`, `src/pitwall/cli.py:451`, `src/pitwall/cli.py:452`).
- Calls `check_domain_config(service)` (the same boot-time domain-config validator `require_runtime_env` uses; see `16-core-config.md`). On `ConfigFileError` / `ValidationError` / `ValueError` it prints `format_settings_load_error(exc)` to stderr and returns `os.EX_CONFIG`. Otherwise it prints `format_config_check_result(result)`; if the result carries errors it goes to stderr and returns `os.EX_CONFIG`, else it prints to stdout and returns `0` (`src/pitwall/cli.py:455`, `src/pitwall/cli.py:456`, `src/pitwall/cli.py:457`, `src/pitwall/cli.py:458`, `src/pitwall/cli.py:460`, `src/pitwall/cli.py:461`, `src/pitwall/cli.py:463`, `src/pitwall/cli.py:464`, `src/pitwall/cli.py:465`).

Argument parser: `_parse_config_args` (`src/pitwall/cli.py:422`, `src/pitwall/cli.py:438`).

| Argument | Required | Default | Type |
|---|---|---|---|
| `check` (subcommand) | Yes | — | literal subcommand (`required=True`) |
| `service` | No | `"api"` | positional service name |

#### `cmd_terminate_pod`

```python
def cmd_terminate_pod(argv: list[str]) -> int
```

- Calls `terminate_pod_sync(pod_id)` then polls `get_pod_sync` until the pod reaches `EXITED`/`TERMINATED`, returns `None` from the RunPod API, or the deadline is exceeded.
- Skips the verification loop if `--no-verify` is set.
- Polling interval: `_TERMINATE_VERIFY_INTERVAL_S` (5 s). Deadline: `--verify-timeout-s` (default 60 s).

Argument parser: `_parse_terminate_pod_args`

| Flag | Required | Default | Type |
|---|---|---|---|
| `--pod-id` | Yes | — | `str` |
| `--no-verify` | No | `False` | `bool` (store_true) |
| `--verify-timeout-s` | No | `60.0` | `float` |

Private helper: `_is_terminated(pod: dict[str, Any]) -> bool` — returns `True` when `pod["desiredStatus"]` is `"EXITED"` or `"TERMINATED"`.

#### `cmd_register_endpoint` / `_register_endpoint_async`

```python
def cmd_register_endpoint(argv: list[str]) -> int
async def _register_endpoint_async(args: argparse.Namespace) -> int
```

- **`cmd_register_endpoint`**: Parses args and wraps `_register_endpoint_async` in `asyncio.run`.
- **`_register_endpoint_async`**: Looks up (or upserts) a `Capability` by name, checks for duplicate provider name, builds a `Provider` record, and calls `ProviderRepository.create`. Sets `openai_base_url` for `SERVERLESS_QUEUE`/`PUBLIC_ENDPOINT` types and `lb_base_url` for `SERVERLESS_LB`. Without `--capability-name`, the supplied `--capability-id` must already exist; otherwise the command exits with a friendly "create it first" error before the DB FK can fail.

Argument parser: `_parse_register_endpoint_args` — 20+ flags including `--endpoint-id`, `--provider-type` (required, choices: `serverless_queue`, `serverless_lb`, `public_endpoint`, `pod_lease`), `--capability-id`, `--capability-name`, `--name`, `--region`, `--gpu-class` (required), `--cost-mode`, `--per-second-active`, `--per-request`, `--per-million-input-tokens`, `--per-million-output-tokens`, `--workers-min`, `--workers-max`, `--idle-timeout-minutes`, `--flash-boot-verified`, `--max-payload-mb`, `--request-timeout-s`, `--priority`, `--health` (default `unknown`; set `healthy` to make the provider immediately routable).

#### `cmd_set_provider_health` / `_set_provider_health_async`

```python
def cmd_set_provider_health(argv: list[str]) -> int
async def _set_provider_health_async(args: argparse.Namespace) -> int
```

- **`cmd_set_provider_health`**: Parses args and wraps `_set_provider_health_async` in `asyncio.run`.
- **`_set_provider_health_async`**: Loads the provider by id, patches `health_status`, and when setting `healthy` also clears failure/cooldown fields so the resolver can route to the provider immediately.

Argument parser: `_parse_set_provider_health_args`

| Argument | Required | Default | Type |
|---|---|---|---|
| `provider_id` | Yes | — | `str` |
| `health` | Yes | — | choice: `unknown`, `healthy`, `unhealthy`, `hibernated` |

#### `cmd_warm_volume` / `_warm_volume_async`

```python
def cmd_warm_volume(argv: list[str]) -> int
async def _warm_volume_async(args: argparse.Namespace) -> int
```

- **`cmd_warm_volume`**: Parses args; prints dry-run parameters if `--dry-run`, otherwise wraps `_warm_volume_async` in `asyncio.run`.
- **`_warm_volume_async`**: Requires `RUNPOD_API_KEY` and `RUNPOD_DATA_CENTER_ID`. Auto-selects the best provider (lowest `priority`, then name, then id) if `--provider` is not given. Builds a base64-encoded pre-warm Python script and injects it via `PREWARM_SCRIPT_B64`. Boots a pod via `create_pod_with_fallback_sync`, polls until exit, then calls `terminate_pod_sync`.

Argument parser: `_parse_warm_volume_args`

| Flag | Required | Default | Type |
|---|---|---|---|
| `--capability` | Yes | — | `str` |
| `--volume-id` | Yes | — | `str` |
| `--provider` | No | `None` (auto-select) | `str` |
| `--script` | No | `"default"` | `str` |
| `--dry-run` | No | `False` | `bool` |
| `--timeout` | No | `1800` | `int` |

Private helper: `_build_prewarm_script(script_name: str, capability: str) -> str` — returns a Python script that prints `PREWARM_START`, the capability, and `PREWARM_COMPLETE`.

#### `cmd_dashboard`

```python
def cmd_dashboard(argv: list[str]) -> int
```

- Parses `pitwall-gpu-broker dashboard` arguments.
- Lazily imports `PitwallApp` from `pitwall.tui`.
- Calls `PitwallApp().run()` and returns `0` after the Textual app exits.

The command is read-only. The Overview and Providers screens support refresh and navigation keys only; future destructive actions must route through the confirmation-tier policy in `pitwall.tui.confirmation`.

#### `cmd_mcp_serve`

```python
def cmd_mcp_serve(argv: list[str]) -> int
```

- Calls `ensure_runtime_env()` from `pitwall.mcp` (validates required env vars for MCP runtime).
- Calls `mcp.run(transport="stdio")` after runtime environment validation.
- `--transport` and `PITWALL_MCP_TRANSPORT` accept only `stdio`; network modes fail closed.

Argument parser: `_parse_mcp_serve_args`

| Flag | Required | Default | Type |
|---|---|---|---|
| `--transport` | No | `"stdio"` (or `PITWALL_MCP_TRANSPORT` env var) | `stdio` only |

---

### `src/pitwall/live.py`

**Responsibility:** Gate that controls whether live RunPod API calls are permitted. Used by smoke scripts and tools that must not run in hermetic environments.

```python
def is_live() -> bool
def require_live() -> None
```

- `is_live()` returns `True` only when **both** conditions hold:
  1. At least one of `RUNPOD_LIVE=1` or `PITWALL_RUN_LIVE=1` is set (truthy values: `1`, `true`, `yes`, `on`).
  2. `PITWALL_BASE_URL` is set to a non-empty, non-whitespace string.
- `require_live()` calls `is_live()`; if `False`, prints an error to stderr and raises `SystemExit(1)`.

---

## 3. Command Inventory

### Global option

Every CLI command accepts `--json` to emit machine-readable JSON instead of human-friendly Rich tables and panels. When `--json` is passed, the command accumulates structured data into a single JSON object written to stdout on success or error.

### Exit codes

| Command | Exit `0` | Exit `1` | Exit `2` | Exit `3` |
|---|---|---|---|---|
| `pitwall-gpu-broker db migrate` | Success | DB error | — | — |
| `pitwall-gpu-broker db reset` | Success | Safety check / DB error | — | — |
| `pitwall-gpu-broker db status` | Success | DB error | — | — |
| `pitwall-gpu-broker init [--from-seed PATH \| --manual] [--non-interactive \| --yes]` | Init complete + provider healthy | Seed validation / no capability or provider / provider health error / exception | — | — |
| `pitwall-gpu-broker create-capability [--spec FILE \| --name NAME --class CLASS --cost-mode MODE]` | Capability created | Flag conflict / missing flags / seed validation / no capabilities / exception | — | — |
| `pitwall-gpu-broker seed PATH... [--mark-healthy]` | Seed applied | Seed validation / provider health error / exception | — | — |
| `pitwall-gpu-broker config check [service]` | Config report (exit 0) | Settings load error / domain-config errors → `EX_CONFIG` | — | — |
| `pitwall-gpu-broker register-template [--dry-run]` | Success | Missing API key / error | — | — |
| `pitwall-gpu-broker terminate-pod [--no-verify]` | Pod confirmed gone | Missing key / error / timeout | — | — |
| `pitwall-gpu-broker register-endpoint` | Success | Missing capability / dup name / DB error | — | — |
| `pitwall-gpu-broker set-provider-health` | Success | Provider not found / DB error | — | — |
| `pitwall-gpu-broker warm-volume [--dry-run]` | Success | Missing env / not found / timeout | All GPU types failed | Terminate failed |
| `pitwall-gpu-broker dashboard` | Textual app launched and exited | Argument parsing / unhandled app error | — | — |
| `pitwall-gpu-broker mcp serve` | Success (blocks) | `ensure_runtime_env` error | — | — |

---

## 4. Public Interfaces (for other subsystems)

### From `cli.py`

| Function | Signature | Who calls it |
|---|---|---|
| `main` | `(argv: list[str] \| None = None) -> int` | `__main__.py`, console script |
| `cmd_init` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_create_capability` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_seed` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_config` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_register_template` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_terminate_pod` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_register_endpoint` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_set_provider_health` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_warm_volume` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_dashboard` | `(argv: list[str]) -> int` | `main()` dispatch |
| `cmd_mcp_serve` | `(argv: list[str]) -> int` | `main()` dispatch |
| `_init_async` | `(args: argparse.Namespace) -> int` | `cmd_init` |
| `_create_capability_async` | `(args: argparse.Namespace) -> int` | `cmd_create_capability` |
| `_seed_async` | `(args: argparse.Namespace) -> int` | `cmd_seed` |
| `_register_template_async` | `(args: argparse.Namespace) -> int` | `cmd_register_template` |
| `_register_endpoint_async` | `(args: argparse.Namespace) -> int` | `cmd_register_endpoint` |
| `_set_provider_health_async` | `(args: argparse.Namespace) -> int` | `cmd_set_provider_health` |
| `_warm_volume_async` | `(args: argparse.Namespace) -> int` | `cmd_warm_volume` |

### From `live.py`

| Function | Signature | Who calls it |
|---|---|---|
| `is_live` | `() -> bool` | Smoke scripts, webhook handlers, lease launch logic |
| `require_live` | `() -> None` | Smoke scripts that must run live |

---

## 5. Configuration

### Env vars read by `cli.py`

| Env var | Command(s) | Default | Purpose |
|---|---|---|---|
| `RUNPOD_API_KEY` | `register-template`, `terminate-pod`, `warm-volume` | *(required)* | RunPod API authentication |
| `RUNPOD_DATA_CENTER_ID` | `warm-volume` | *(required)* | RunPod data center for pod placement |
| `PITWALL_CLOUD_WORKER_IMAGE` | `warm-volume` | *(required)* | Image for warm-up pod |
| `HUGGINGFACE_TOKEN` | `warm-volume` | *(optional)* | Passed to warm-up pod env if set |
| `DATABASE_URL` | `dashboard` | *(required unless a test source is injected)* | Postgres pool DSN for Overview state reads |
| `PITWALL_MCP_TRANSPORT` | `mcp serve` | `"stdio"` | MCP transport override |

### Env vars read by `live.py`

| Env var | Alias | Purpose |
|---|---|---|
| `RUNPOD_LIVE` | `PITWALL_RUN_LIVE` | Opt-in flag for live RunPod calls |
| `PITWALL_BASE_URL` | — | Base URL of the Pitwall server; required in addition to the opt-in flag |

---

## 6. Failure Modes & Error Types

### CLI argument parsing errors
`argparse.ArgumentError` (or a non-zero return from argparse itself) when required flags are missing or values fail type conversion. These print to stderr and return `1`.

### Missing environment variables
All non-dry-run commands that call the RunPod API require `RUNPOD_API_KEY`. `warm-volume` additionally requires `RUNPOD_DATA_CENTER_ID` and `PITWALL_CLOUD_WORKER_IMAGE`. Missing keys produce a line like `"RUNPOD_API_KEY environment variable is not set"` to stderr and return `1`.

### `cmd_init` failure modes
- `SeedValidationError` from seed file loading or manual seed application: prints `"ERROR: ..."`, returns `1` (`src/pitwall/cli.py:525`, `src/pitwall/cli.py:536`).
- No capability, no provider, or a failed provider health lookup after seed application: prints descriptive error, returns `1` (`src/pitwall/cli.py:540`, `src/pitwall/cli.py:543`, `src/pitwall/cli.py:548`).
- Unhandled async exception: printed as `"Error: ..."`, returns `1` (`src/pitwall/cli.py:686`, `src/pitwall/cli.py:691`, `src/pitwall/cli.py:693`).

### `cmd_create_capability` failure modes
- `--spec` combined with manual capability flags; missing `--name`, `--class`, or `--cost-mode`; empty capability name: prints `"ERROR: ..."`, returns `1` (`src/pitwall/cli.py:292`, `src/pitwall/cli.py:313`, `src/pitwall/cli.py:324`).
- Seed validation or an empty spec result in `--spec` mode: prints `"ERROR: ..."`, returns `1` (`src/pitwall/cli.py:296`, `src/pitwall/cli.py:303`, `src/pitwall/cli.py:306`).
- Unhandled async exception: printed as `"Error: ..."`, returns `1` (`src/pitwall/cli.py:351`, `src/pitwall/cli.py:356`, `src/pitwall/cli.py:358`).

### `cmd_seed` failure modes
- `SeedValidationError` from missing paths, empty directories, invalid seed shapes, enum validation, duplicate provider names, or provider config validation: prints `"ERROR: ..."`, returns `1` (`src/pitwall/cli.py:386`, `src/pitwall/cli.py:395`, `src/pitwall/seed.py:70`, `src/pitwall/seed.py:91`, `src/pitwall/seed.py:252`, `src/pitwall/seed.py:183`, `src/pitwall/seed.py:352`, `src/pitwall/seed.py:497`).
- `--mark-healthy` provider health lookup failure: prints `"ERROR: Provider not found: ..."`, returns `1` (`src/pitwall/cli.py:389`, `src/pitwall/cli.py:392`).
- Unhandled async exception: printed as `"Error: ..."`, returns `1` (`src/pitwall/cli.py:410`, `src/pitwall/cli.py:415`, `src/pitwall/cli.py:417`).

### `cmd_terminate_pod` failure modes
- `RuntimeError` from `terminate_pod_sync`: prints error + `"Manual teardown via RunPod console required."` and returns `1`.
- Verification timeout: prints `"did not reach EXITED/TERMINATED within Ns"` + `"Manual verification via RunPod console required."` and returns `1`.
- `get_pod_sync` raising during polling: warns and continues (does not fail the command).

### `cmd_register_endpoint` failure modes
- Duplicate provider name: `"ERROR: Provider with name 'X' already exists (id=prov_...)"` to stderr, returns `1`.
- `RuntimeError` / exception from `asyncio.run`: caught, printed to stderr as `"Error: ..."`, returns `1`.

### `cmd_warm_volume` failure modes
- `create_pod_with_fallback_sync` fails for all GPU types: returns `2`.
- `terminate_pod_sync` fails after pod exit: returns `3`.
- Capability or provider not found: prints descriptive error, returns `1`.

### `cmd_dashboard` failure modes
- Argument parsing errors are handled by `argparse`.
- If the Overview state source raises during refresh, the screen shows a generic `"Overview unavailable: state source failed"` message and keeps the shell running. The message is intentionally generic so DSNs and other sensitive details are not reflected into the UI.
- If the Providers source raises during refresh, the screen shows a generic `"Providers unavailable: registry source failed"` message and keeps the shell running. The message is intentionally generic so provider configuration and credential details are not reflected into the UI.
- Without an injected test source, the default app path resolves the normal Pitwall DB pool, so `DATABASE_URL` must be configured before the Overview can read broker state.

### `live.py`
- `require_live()` raises `SystemExit(1)` with a descriptive message when live mode is not active.

---

## 7. Testing

| File | What it covers |
|---|---|
| `tests/cli/test_cli_dispatch.py` | Characterization suite (1338+ lines). Dispatch routes for DB, `register-template`, `terminate-pod`, `register-endpoint`, `set-provider-health`, `warm-volume`, MCP, and `config check`; argument parser defaults, dry-run output, terminate verification loop (timeout, pod gone, already exited, poll error, waiting message), async exception handlers, warm-volume dry-run, capability/provider not-found, duplicate provider name, cost mode, serverless LB/public endpoint provider types, MCP serve transport dispatch, `_is_terminated` truth table. Includes `--json` flag validation for `register-template`, `warm-volume`, `terminate-pod`, and `config check`. |
| `tests/cli/test_cli_output.py` | Output layer unit tests: plain text / JSON mode switching, table/panel/error/warning/success rendering, JSON emission, `add_json_argument` / `json_mode` helpers, `_safe_json` serialization for Pydantic models and plain objects. |
| `tests/cli/test_init_seed.py` | Onboarding command helpers: `create-capability` happy path and blank-name rejection, seed file application, and `init --from-seed --non-interactive` marking the created provider healthy. |
| `tests/tui/test_overview.py` | Textual Overview coverage: snapshot formatting, property-based status normalization, confirmation-tier stubs, Pilot mount/refresh tests with `StaticOverviewSource`. |
| `tests/tui/test_providers_screen.py` | Textual Providers coverage: default registry enumeration, snapshot summary pluralization, Pilot navigation from Overview with `p`, and row rendering with `StaticProvidersSource`. |
| `tests/tui/test_cli_dashboard.py` | Hermetic `pitwall-gpu-broker dashboard` dispatch coverage with `PitwallApp.run` monkeypatched. |
| `tests/test_live.py` | `is_live()` truth table (no env, only live flag, only base URL, both, alias var, truthy/falsy values, empty base URL); `require_live()` exit behavior. |
| `tests/runpod_client/test_cli.py` | Subprocess-level tests for `register-template`: missing API key exit, dry-run output for GHCR/GitLab/Docker Hub/unknown registries (auth selection), missing `--image` error, image/template detail output. Async tests: cache hit, template creation, missing API key. |
| `tests/runpod_client/test_warm_volume.py` | `_warm_volume_async` — tests auto-provider selection, explicit provider, missing volume ID handling. |
| `tests/db/test_reset_safety.py` | `cmd_reset` from `pitwall.db` is invoked via `pitwall-gpu-broker db reset`; tests the single-drop invariant. |
| `tests/db/test_asyncpg_migrate.py` | `cmd_migrate` async path with mocked pool: pending migration execution, skip already-tracked, error handling. |

---

## 8. Dependencies

### From `pitwall` (internal)

| Module | Used by | Purpose |
|---|---|---|
| `pitwall.runpod_client.templates` | `cli.py` | `ensure_template`, `get_registry_auth_id_from_env`, `image_sha`, `normalize_template_name`, `template_display_name`, `_TEMPLATE_ENV_KEYS` |
| `pitwall.runpod_client.pods` | `cli.py` | `terminate_pod_sync`, `get_pod_sync`, `create_pod_with_fallback_sync` |
| `pitwall.runpod_client.gpu` | `cli.py` | `validate_canonical_gpu_name` |
| `pitwall.runpod_client.workloads` | `cli.py` | `WorkloadConfig` |
| `pitwall.seed` | `cli.py` | `SeedValidationError`, `apply_seed_files`, `apply_seed_data`, `apply_capability_seed_files` |
| `pitwall.db` | `cli.py` | `get_pool`, `main` (for `pitwall-gpu-broker db` subcommands) |
| `pitwall.db.repository` | `cli.py` | `CapabilityRepository`, `ProviderRepository` |
| `pitwall.core.enums` | `cli.py` | `CapabilityClass`, `CapabilityHint`, `CostMode`, `ProviderType`, `CapabilitySource` |
| `pitwall.core.ids` | `cli.py` | `ulid_new` |
| `pitwall.core.models` | `cli.py` | `Provider` |
| `pitwall.tui` | `cli.py` | Textual dashboard app (`PitwallApp`) |
| `pitwall.core.cost_reporting` | `tui.overview` | Read-only cost and workload summary queries |
| `pitwall.db.repository` | `tui.overview` | Provider reads for Overview state |
| `pitwall.providers.registry` | `tui.providers` | Read-only provider plugin enumeration via `get_default_registry` / injected registry factory |
| `pitwall.mcp` | `cli.py` | `ensure_runtime_env`, `mcp` (FastMCP instance) |

### External libraries

| Library | Version constraint | Purpose |
|---|---|---|
| `argparse` | stdlib | CLI flag parsing |
| `asyncio` | stdlib | Async command wrappers |
| `base64` | stdlib | Pre-warm script encoding |
| `json` | stdlib | Init smoke-command payload formatting, JSON mode emission |
| `time` | stdlib | Polling loops |
| `rich` | `>=15.0` (transitive via `textual`) | Tables, panels, and styled console output (`cli_output.py`) |
| `textual` | `>=8.2,<9` | TUI app shell, widgets, key bindings, Footer, and Pilot tests |
| `mcp` | `>=1.0.0` (from `pitwall.mcp`) | MCP server (`cmd_mcp_serve`) |

---

## 9. TUI Pods / Leases View

`src/pitwall/tui/leases.py` adds the read-only Pods / Leases screen to the existing
Textual `pitwall-gpu-broker dashboard` shell. It follows the same source-injection pattern as the
Overview screen: production code uses `PostgresLeasesSource`, while tests inject
`StaticLeasesSource` and never open Postgres or RunPod connections.

The screen is reached with `l` from `PitwallApp`; `o` returns to Overview, `r` refreshes
the active screen, and `q` / `Ctrl-C` still quit through the app shell. Navigation is
read-only. There are no terminate, stop, renew, or provider mutation widgets on this
screen.

`PostgresLeasesSource` reads persisted active pod lease rows from `pitwall.leases`,
excluding terminal states (`stopped`, `failed`, `expired`). It displays the lease ID,
RunPod pod ID, provider ID, lease state, persisted readiness label, UTC expiry, and
accrued cost. Readiness is derived from the persisted readiness JSON: all runtime,
port-mapping, and probe timestamps produce `ready`; a subset produces `partial`; no
signals produce `pending`.

Hermetic coverage lives in `tests/tui/test_leases_screen.py`:

| Test area | Coverage |
|---|---|
| formatting | Cost rounding, missing-cost label, readiness labels, UTC timestamp rendering |
| property | Status normalization always produces a non-empty display-safe key |
| source | `StaticLeasesSource` load counting for refresh assertions |
| Pilot | `l` navigation, table rows, empty state, refresh, source-failure rendering, `o` back to Overview |
## Addendum: TUI Cost View

`pitwall-gpu-broker dashboard` now includes a read-only Cost screen alongside Overview and Providers. The global Textual bindings are `o` Overview, `p` Providers, `c` Cost, `r` refresh on the active screen, and `q` / `Ctrl-C` quit.

`src/pitwall/tui/cost.py` owns the Cost screen. It follows the existing source/snapshot/static-source pattern:

| Object | Responsibility |
|---|---|
| `CostScreen` | Textual screen with runway, sub-budget chargeback, what-if summary, refresh, and generic source-failure messaging |
| `CostSnapshot` | Immutable view model built from `BurnRateForecast`, `ChargebackReport`, and `WhatIfBatchProjection` |
| `CostSource` | Async protocol for loading one Cost refresh |
| `StaticCostSource` | Hermetic test/demo source |
| `PostgresCostSource` | Lazy Postgres-backed source for dashboard runtime |

Runtime data flow:

- Runway uses `pitwall.finops.burn_rate.forecast_from_cost_daily` over `pitwall.cost_daily`.
- Sub-budget rows use `pitwall.cost.sub_budgets.generate_chargeback_report` over current-month workload cost rows. Tags are resolved from workload `budget_tag`, `tag`, or `team` values when present.
- What-if renders a `pitwall.cost.simulator.WhatIfBatchProjection` summary. If no projection inputs are configured, the screen renders an empty read-only projection with current spend, budget headroom, and zero reserved spend.
- `PostgresCostSource` is lazy: `PitwallApp` installs the screen at mount, but the Cost source does not resolve the DB pool until the Cost screen is opened or refreshed.

Cost failure modes:

- If the Cost source raises during refresh, the screen shows `"Cost unavailable: state source failed"` and keeps the shell running. The message is intentionally generic so database URLs, provider configuration, and other sensitive details are not reflected into the UI.
- Without an injected source, the Cost screen needs `DATABASE_URL` for broker state and `PITWALL_MONTHLY_BUDGET_USD` for runway/headroom calculations.

Cost TUI coverage:

| File | What it covers |
|---|---|
| `tests/tui/test_cost_screen.py` | Cost snapshot summaries, runway day formatting, property-based percent bounds, sub-budget table rendering, `StaticCostSource`, Pilot navigation via `c`, screen rendering, refresh, and generic source-failure messaging |

Additional internal dependencies:

| Module | Used by | Purpose |
|---|---|---|
| `pitwall.finops.burn_rate` | `tui.cost` | Runway forecast and `forecast_from_cost_daily` adapter |
| `pitwall.cost.sub_budgets` | `tui.cost` | Chargeback line items, reports, and sub-budget report generation |
| `pitwall.cost.simulator` | `tui.cost` | What-if batch projection summary |

## Addendum: TUI Resources View

`pitwall-gpu-broker dashboard` now includes a read-only Resources screen for RunPod account
inventory. The global Textual bindings are `o` Overview, `p` Providers, `l`
Leases, `c` Cost, `e` Resources, `r` refresh on the active screen, and `q` /
`Ctrl-C` quit.

`src/pitwall/tui/resources.py` owns the Resources screen. It follows the sibling
source/snapshot/static-source pattern:

| Object | Responsibility |
|---|---|
| `ResourcesScreen` | Textual screen with fixed-width Endpoints, Templates, Volumes, and Registry auth tables plus refresh and generic source-failure messaging |
| `ResourcesSnapshot` | Immutable view model with endpoint, template, network-volume, registry-auth, and UTC refresh state |
| `ResourcesSource` | Async protocol for loading one Resources refresh |
| `StaticResourcesSource` | Hermetic test/demo source |
| `RunPodResourcesSource` | Read-only RunPod resource source composed from existing client list APIs |

Runtime data flow:

- Endpoints use `pitwall.runpod_client.serverless.list_endpoints` and render only
  endpoint ID, name, worker min/max, and template ID.
- Templates use `pitwall.runpod_client.templates.list_hub_templates` and render
  template ID, display/name, image name, and serverless flag.
- Volumes use `pitwall.runpod_client.mounts.NetworkVolumeClient.list` and render
  volume ID, name, size in GB, and datacenter ID.
- Registry auths use `pitwall.runpod_client.registry.list_container_registry_auths`
  and render only auth ID and name. The RunPod model does not expose usernames or
  passwords after credential creation.

Resources failure modes:

- If any Resources source dependency raises during refresh, the screen shows
  `"Resources unavailable: state source failed"` and keeps the shell running. The
  message is intentionally generic so API keys, provider configuration, and other
  sensitive details are not reflected into the UI.
- Without an injected source, the Resources screen needs the normal RunPod
  runtime environment required by the underlying read-only client APIs.
- The screen does not render raw API URLs or raw response payloads; it displays
  sanitized scalar labels only.

Resources TUI coverage:

| File | What it covers |
|---|---|
| `tests/tui/test_resources_screen.py` | Resources snapshot summary, display-label property, fixed-width resource tables, injected read-only client mapping, Pilot navigation via `e`, screen rendering, refresh, and generic source-failure messaging |

## Addendum: TUI Operations View

`pitwall-gpu-broker dashboard` now includes a read-only Operations screen for the final
Initiative-2 operator summary. The global Textual bindings are `o` Overview,
`p` Providers, `l` Leases, `c` Cost, `e` Resources, `a` Operations, `r`
refresh on the active screen, and `q` / `Ctrl-C` quit.

`src/pitwall/tui/operations.py` owns the Operations screen. It follows the
sibling source/snapshot/static-source pattern:

| Object | Responsibility |
|---|---|
| `OperationsScreen` | Textual screen with Catalog, Jobs, Resilience, Routing, Policies, and Autopilot summaries plus refresh and generic source-failure messaging |
| `OperationsSnapshot` | Immutable view model with catalog rows, job state counts, recent jobs, provider resilience rows, routing/policy/autopilot summaries, and UTC refresh state |
| `OperationsSource` | Async protocol for loading one Operations refresh |
| `StaticOperationsSource` | Hermetic test/demo source |
| `PostgresOperationsSource` | Read-only Postgres-backed source composed from existing repositories, pure routing, policy, and autopilot layers |

Runtime data flow:

- Catalog uses `CapabilityRepository.list` and `ProviderRepository.list` to
  render capability ID, name, class, cost mode, enabled state, and provider
  count.
- Jobs use narrow read-only SQL over `pitwall.workloads` to render state counts
  and recent workload rows. The screen displays IDs, state, UTC submission time,
  and rounded cost only.
- Resilience uses provider health, consecutive failures, cooldown trips,
  cooldown timestamp, and recent error rate from persisted provider rows.
- Routing uses the existing pure `plan_route` / `RoutingRequest` stack for the
  first capability with providers. It renders selected provider, fallback chain,
  candidate count, eliminated count, and capacity decision count.
- Policies use `load_default_policy_set` and `evaluate_policies` against a
  sanitized in-memory snapshot of capabilities, providers, and recent jobs.
- Autopilot uses `AutopilotController` in shadow mode with the existing
  `WhatIfSimulator` and deterministic `PlanningContext`. The default dashboard
  path supplies no action signals; injected sources can render non-empty
  decision summaries hermetically.

Operations failure modes:

- If the Operations source raises during refresh, the screen shows
  `"Operations unavailable: state source failed"` and keeps the shell running.
  The message is intentionally generic so database URLs, provider configuration,
  policy payloads, and other sensitive details are not reflected into the UI.
- Without an injected source, the Operations screen needs the normal Pitwall
  database runtime environment used by the underlying read-only state queries.
- The screen does not render raw provider config, raw workload input, raw policy
  violation payloads, raw API URLs, or secrets.

Operations TUI coverage:

| File | What it covers |
|---|---|
| `tests/tui/test_operations_screen.py` | Operations snapshot summaries, display-label property, fixed-width catalog/jobs/resilience tables, injected read-only source mapping, Pilot navigation via `a`, screen rendering, refresh, and generic source-failure messaging |
