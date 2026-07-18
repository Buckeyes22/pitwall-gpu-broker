# Testing Strategy

Pitwall uses layered verification for unit behavior, database and Redis integration,
concurrency, security, API contracts, mutation resistance, packaging, containers, and
public-alpha release readiness. GitHub Actions is the only supported public CI platform.

## 1. Markers and isolation

Markers are registered in `pyproject.toml`.

| Marker | Purpose | External dependency |
| --- | --- | --- |
| *(none)* | Hermetic unit and contract tests | None |
| `integration` | Real PostgreSQL/Redis behavior | Test Compose stack |
| `property` | Hypothesis invariants | None |
| `chaos` | Fault-injection and degraded paths | None unless also `live` |
| `security` | Authentication and security regressions | None |
| `fuzz` | Schemathesis/Hypothesis fuzzing | None |
| `benchmark` | Micro-benchmarks | None |
| `slow` | Long-running load or mutation-related work | Varies |
| `release` | Public-alpha release envelopes | Exact `-m release` selector |
| `live` | Real RunPod calls | Explicit opt-in and credentials |

Hermetic tests set placeholder values through `tests/conftest.py`; they must not call RunPod or
require local infrastructure. Integration tests are skipped unless their test DSNs are present.
Live tests are skipped unless `--run-live` or `RUNPOD_LIVE=1` is explicit.

The release fixture intentionally enables release envelopes only when pytest receives the exact
marker expression `-m release`. Do not replace it with `-m "release and not live"`; live envelopes
already self-skip when live execution is not enabled.

## 2. Verification tracks

| Track | What it proves |
| --- | --- |
| Unit and contract | Deterministic behavior, API envelopes, CLI dispatch, and regressions |
| Property | Pure-logic invariants across generated inputs |
| Integration | Migrations, repositories, transactions, Redis, backup/restore, and concurrency |
| API security | Auth scope matrix, body bounds, SSRF defenses, webhook controls, and redaction |
| API fuzz | Every generated OpenAPI operation avoids unexpected server errors |
| Chaos | Retry, outage, termination, and safe-degradation behavior |
| Mutation | High-risk cost, rate-limit, and lease-state oracles reject behavioral mutants |
| Release | Artifacts, operational journeys, strict audit, and publication policy |
| Operator live | Optional real-provider validation using operator-owned credentials and resources |

Production bug fixes require a regression test at the lowest meaningful layer. Database
correctness, locking, migrations, and restore behavior require real-PostgreSQL integration tests;
mock-only evidence is insufficient for those paths.

## 3. Local infrastructure

`docker-compose.testinfra.yml` provides PostgreSQL 16 on `127.0.0.1:5444` and Redis 7 on
`127.0.0.1:6380`. The integration fixtures apply the packaged SQL migrations in discovery order.

```bash
make up
make test-int
make down
```

Use a unique `COMPOSE_PROJECT_NAME` when multiple checkouts or CI jobs share one Docker daemon.

## 4. Security and API compatibility

The security chain includes:

- Bandit with a reviewed baseline;
- pip-audit over the frozen runtime dependency graph;
- deterministic detect-secrets review and drift detection;
- license allow/deny/review policy over the runtime graph;
- Semgrep registry rules plus the repository-local policy;
- CodeQL in GitHub Actions;
- security-marked regression tests and all-operation Schemathesis fuzzing;
- OpenAPI compatibility comparison against `docs/api/openapi-baseline.json`.

The Schemathesis test derives its operation set from the application schema. It exercises all 43
current operations, including protected and health routes, and fails on unexpected 5xx responses.
The committed OpenAPI compatibility gate separately rejects removal or incompatible mutation of
existing methods, parameters, request bodies, or successful response schemas.

```bash
make sec
make sec-semgrep
make sec-test
make sec-fuzz
make openapi-check
```

## 5. Mutation and performance

`make mutation-gate` runs mutmut over the configured high-risk pure-logic modules, exports CI
statistics, and requires at least an 85% kill score among covered mutants. `make bench` runs
micro-benchmarks; `make load-smoke` validates the load profile without calling a deployed service.
Actual load execution requires an operator-supplied `PITWALL_HOST` and remains an explicit action.

## 6. Coverage

CI maintains two coverage floors:

- the hermetic `test` job enforces 74%;
- `coverage-combined` appends the hermetic and integration lanes and enforces 77%.

These are minimum ratchets, not quality claims. Increase them as meaningful branch coverage grows;
do not lower them to land a change.

The combined lane also evaluates weighted line and branch floors for authorization, spending,
webhooks, leases, retention/deletion, R2 cleanup, and the worker boundary. The versioned policy is
`tools/ci/risk-coverage-policy.json`; a missing source match fails rather than silently dropping a
risk domain.

## 7. CI and release readiness

`.github/workflows/ci.yml` runs `lint`, `format`, `typecheck`, `security-sast`,
`security-secrets`, `security-fuzz`, `mutation-smoke`, `test`, `integration`, and
`coverage-combined`. The mutation smoke lane is diagnostic; the blocking mutation floor runs in
`release-readiness.yml`.

`.github/workflows/release-readiness.yml` provides blocking security, mutation, and hermetic
jobs. The hermetic job validates the strict sixteen-check audit, exact release-marker suite,
candidate policy, OpenAPI compatibility, artifacts, and combined coverage. The workflow neither
requests provider credentials nor enables live execution. Live tests are separate operator-side
checks using credentials and resources owned by the deploying operator.

The release workflow publishes only after readiness succeeds. It rebuilds Python artifacts twice,
checks byte identity, validates wheel/sdist contents, signs artifact/image provenance through
GitHub attestations, generates SBOMs, scans images, and promotes the exact tested digests. The
deferred TestPyPI/PyPI channel runs only when its separate enable variable is deliberately set.

## 8. Commands

```bash
# Fast hermetic lane
uv run pytest -q -m "not integration and not slow"

# Real PostgreSQL/Redis integration lane
make up
make test-int
make down

# Security and mutation
make sec-test
make sec-fuzz
make mutation-gate

# Exact release envelopes and strict audit
uv run pytest -q -m release tests/release
uv run python -m pitwall.audit.sixteen_check --strict

# Full local public-alpha rehearsal (requires DATABASE_URL and REDIS_URL)
scripts/release/run-alpha-readiness.sh
```

See `docs/operator/release-testing-checklist.md` for the human-run checklist and
`docs/release/external-release-gates.md` for evidence that cannot be produced inside this checkout.
