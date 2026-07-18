# Contributing to Pitwall

Welcome! Pitwall is a self-hosted RunPod workload broker. This
document covers everything you need to start contributing.

## Dev Environment

```bash
# Clone the repo
git clone https://github.com/your-fork/pitwall-gpu-broker.git
cd pitwall-gpu-broker

# Create the venv and install all dependencies (including dev)
uv sync --frozen --extra dev

# Python 3.12 and 3.13 are supported
uv run python --version
```

## Running Tests

### Hermetic lane (no GPU billing, no external services)

All unit/integration tests that do not touch real cloud infrastructure run under
the hermetic lane. This is the default `test` target and avoids any RunPod spend.

```bash
make test          # alias for test-fast
```

`test-fast` runs pytest with `-m "not integration and not slow"` — no Postgres,
no Redis, no external network.

### Integration lane (requires local Postgres + Redis)

The integration suite needs a running Postgres 5444 and Redis 6380. Use the
testinfra compose to bring them up:

```bash
make up            # docker compose -f docker-compose.testinfra.yml up -d
# wait a moment for the databases to be ready
make test-int      # PITWALL_TEST_DATABASE_URL=... PITWALL_TEST_REDIS_URL=... pytest -q -m integration
make down          # docker compose -f docker-compose.testinfra.yml down
```

## Quality Gates

Run all gates before pushing. CI enforces every one of these.

| Gate | Make target | What it does |
|---|---|---|
| Unit tests | `make test` | Hermetic pytest (`-m "not integration and not slow"`) |
| Integration tests | `make up && make test-int && make down` | Real PostgreSQL and Redis behavior |
| Coverage report | `make test-cov` | pytest + coverage HTML report |
| Security scan | `make sec` | bandit SAST + pip-audit CVE check |
| Semgrep | `make sec-semgrep` | Registry rules plus repository-local policy |
| Security unit tests | `make sec-test` | pytest `-m "security and not fuzz"` |
| API fuzzing | `make sec-fuzz` | schemathesis `-m fuzz` |
| Mutation testing | `make mutation-gate` | mutmut run + export-cicd-stats + >=85% kill floor |
| Benchmark smoke | `make load-smoke` | pytest `-m slow` (locustfile import check) |
| Combined coverage | fast + integration `coverage run` | ratchet `fail_under=77` (see `docs/sdlc/17-testing-strategy.md` §8) |
| Pattern scrub | `python tools/guards/repo_text_policy.py <files>` | banned literals, real RunPod IDs/hosts |
| OpenAPI compatibility | `make openapi-check` | no incompatible public API change |
| Workflow policy | `make ci-tools` | workflow syntax, trust, release, and DCO policy |

The table above is the local pre-push set; `docs/sdlc/17-testing-strategy.md`
§10 is the authoritative CI matrix (adds the property, chaos, and
release-readiness tiers).

## Definition of Done

Two non-negotiable standards apply to **every** change and are enforced at PR
review. They are the bar this project was built to — do not regress them.

### 1. Documentation parity (SDLC docs)

The `docs/sdlc/` chapters are the source-grounded technical map of Pitwall and
**must not drift**. Any PR that changes behavior updates the relevant chapter(s)
in the **same PR** as the code:

- Changed subsystem behavior → update its chapter. A genuinely new subsystem
  gets a new numbered chapter plus an index row in `docs/sdlc/README.md`.
- Cite real source as `path:line` and verify each citation against the code you
  are shipping. A doc that disagrees with the code is a defect, not a nit.
- Keep the README and the `00-overview` / `14-security` trust-model claims true.
- The `repo_text_policy` pattern gate stays green on changed files.

A code change with no corresponding doc update is **incomplete** and should not
be approved.

### 2. Testing parity (the comprehensive program)

`docs/sdlc/17-testing-strategy.md` defines the authoritative verification program;
every change lives up to it as applicable to the surface it touches:

- **Hermetic** unit/contract tests for new logic (default lane, no infra).
- **Property** tests (Hypothesis) for pure-logic invariants.
- **Integration** tests for any Postgres/Redis path.
- **Chaos** tests for new failure / timeout / partial-outage paths.
- **Security** tests for any auth / SSRF / secret / HMAC surface.
- **Mutation** kill-floor (≥85%) holds for the core pure-logic trio.
- **Find → fix:** every production bug ships with a hermetic regression test.
- The **combined fast+integration coverage** ratchet (`fail_under=77`) holds —
  raise it as coverage grows, never lower it.

## PR Process

1. **Branch** — create a feature or fix branch:
   ```bash
   git checkout -b feat/your-feature-name
   # or
   git checkout -b fix/short-description
   ```

2. **Keep gates green** — run `make test` and `make sec` locally before pushing.
   CI runs the full suite; do not merge with failing gates.

3. **CI checks** — required checks include lint, format, strict types, SAST, secret/license
   policy, API fuzzing, hermetic tests, real-infrastructure integration, and combined coverage.
   The public-alpha release workflow additionally requires the full mutation gate, artifact
   checks, strict audit, release envelopes, and live acceptance. Repository settings define the
   exact protected check names.

4. **Commit messages** — use clear, concise subject lines. Conventional-ish format
   is recommended:
   ```
   feat: add cost estimation for GPU leasing
   fix: handle Redis connection pool exhaustion
   chore: update pip-audit baseline
   ```

5. **DCO sign-off** — every commit must be signed. Add `-s` to the commit command:
   ```bash
   git commit -s -m "fix: handle nil pointer in lease state"
   ```
   The DCO sign-off certifies you have the right to contribute the code.

## Code of Conduct

Pitwall follows the [Contributor Covenant](https://www.contributor-covenant.org/).
Please be respectful and constructive in all interactions.
