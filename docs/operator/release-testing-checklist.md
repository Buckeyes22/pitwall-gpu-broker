# Pitwall Release-Testing Checklist (Operator)

The public-alpha gate is the `release-readiness.yml` workflow plus
`scripts/release/run-alpha-readiness.sh`.
This checklist is the human-run companion: what each gate proves and how to run it locally
before cutting a release.

## 0. Prerequisites
- Postgres + Redis test infra up: `make up` (Postgres `127.0.0.1:5444`, Redis `127.0.0.1:6380`).
- Dependencies installed: `uv sync --frozen --extra dev`.

## 1. Static gates (must be green)
| Gate | Command | Proves |
|------|---------|--------|
| Lint | `uv run ruff check .` | no lint regressions |
| Format | `uv run ruff format --check .` | canonical formatting |
| Types | `uv run mypy --strict src/` | no type regressions |

## 2. Sixteen-check audit — FATAL
```bash
uv run python -m pitwall.audit.sixteen_check --strict
```
A non-zero exit blocks the release. (`--json` emits the machine-readable report the CI
workflow uploads as an artifact.)

## 3. Release tiers — EXACT `-m release`
```bash
uv run pytest -m release tests/release -q
```
> **Critical:** `tests/release/conftest.py` un-skips the tier tests ONLY when the pytest
> `-m` option is the exact string `release`. `-m "release and not live"` silently skips the
> entire release gate. The `live`-marked envelopes (BGE-M3 smoke, kill drill) self-skip
> without `RUNPOD_LIVE=1`; that is expected for a hermetic cut. Project CI never supplies
> provider credentials or enables live execution.

Tiers: **dry-run** (routing + cost estimation, no spend), **sovereignty** (region/residency),
**BGE-M3 smoke** (`live`), **kill drill** (`live`).

## 4. Kill-switch drill — <30s, atomic, ordered
The kill switch must complete in `< 30s` with stage order ACL-deny → device-removal →
compute-termination. Hermetic proof:
```bash
uv run pytest -q tests/admin/test_kill_switch.py tests/admin/test_kill_switch_route.py
```
The middleware 401 gate (missing/wrong `X-Pitwall-Secret` → 401) is covered there too.

## 5. Coverage — combined floor 77% (long-term target 95%)
Hermetic-only coverage tops out ~74% (real-DB modules aren't exercised without Postgres);
adding the integration suite brings the combined total to ~77.6%. The alpha floor is enforced
on the COMBINED fast + integration run at **77%** (an honest ratchet above the 74% hermetic
floor). 95% remains the long-term target as broad-coverage tests are added; raise this floor
as coverage grows, never lower it.
```bash
uv run coverage erase
uv run coverage run --append -m pytest -p no:randomly -p no:cov \
  -m "not integration and not slow and not live"
DATABASE_URL=postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test \
PITWALL_TEST_DATABASE_URL=postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test \
PITWALL_TEST_REDIS_URL=redis://127.0.0.1:6380/0 \
  uv run coverage run --append -m pytest -p no:randomly -p no:cov -m integration tests
uv run coverage report --include="src/**" --fail-under=77
```

## 6. Security + mutation (program gates)
```bash
make sec-test && make sec-fuzz        # admin auth / SSRF / webhook HMAC / fuzz
make mutation-gate                    # >=85% mutation kill on the core trio
```

## 7. Full public-alpha harness
```bash
DATABASE_URL=... REDIS_URL=... scripts/release/run-alpha-readiness.sh
```
Runs the `-m release` tier suite **and** the fatal `--strict` sixteen-check; non-zero exit
means a gate failed. Artifacts land under `dist/alpha-artifacts/`.

## Sign-off
- [ ] Sections 1–6 green locally.
- [ ] `release-readiness.yml` green on the release PR.
- [ ] `run-alpha-readiness.sh` exit 0 against staging infra.
- [ ] Optional live tiers, if selected by a deploying operator, use only that operator's
      local credentials, endpoints, budget, and cleanup procedure; they do not gate the
      project release.
