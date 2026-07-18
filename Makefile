SHELL := /bin/bash

.PHONY: test test-fast test-int test-cov up down sec sec-baseline sec-semgrep sec-test sec-fuzz license-check openapi-check docs-check ci-tools mut mutation mutation-gate bench load load-smoke
test: test-fast
up:        ; docker compose -f docker-compose.testinfra.yml up -d
down:      ; docker compose -f docker-compose.testinfra.yml down
test-fast: ; uv run pytest -q -m "not integration and not slow"
test-int:  ; PITWALL_TEST_DATABASE_URL=postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test PITWALL_TEST_REDIS_URL=redis://127.0.0.1:6380/0 uv run pytest -q -m integration
test-cov:  ; uv run pytest -q -m "not integration and not slow" --cov=src/pitwall --cov-report=term-missing --cov-report=html
sec: ## SAST + dependency CVE + secrets scan (no network for bandit)
	uv run bandit -c pyproject.toml -r src/pitwall -ll -ii \
		-b tools/security/bandit-baseline.json
	uv run pip-audit -r <(uv export --frozen --format requirements-txt --no-hashes)
	uv run python tools/security/check_secrets.py
	uv run python tools/security/check_licenses.py
sec-baseline: ## Regenerate the bandit baseline (run only after triaging each new finding)
	uv run bandit -c pyproject.toml -r src/pitwall -ll -ii \
		-f json -o tools/security/bandit-baseline.json || test $$? -eq 1
sec-semgrep: ## Local Semgrep Python + security audit rules (downloads registry rules; metrics disabled)
	SEMGREP_SEND_METRICS=off semgrep scan --error --config p/python --config p/security-audit \
		--exclude-rule python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5 \
		--config tools/security/semgrep.yml src/pitwall
ci-tools: ## Validate GitHub workflow syntax, trust policy, and local release policy
	actionlint
	uv run python tools/ci/check_workflows.py
	uv run pytest -q -m release tests/release/test_release_policy.py tests/release/test_dco_policy.py
openapi-check: ## Export and compare the current API with the committed baseline
	uv run python tools/ci/export_openapi.py --output /tmp/pitwall-openapi.json
	uv run python tools/ci/check_openapi_compat.py docs/api/openapi-baseline.json /tmp/pitwall-openapi.json
docs-check: ## Validate all repository-local Markdown links and anchors
	uv run python tools/ci/check_markdown_links.py
license-check: ## Inventory the installed runtime graph and enforce license policy
	uv run python tools/security/check_licenses.py
sec-test: ## Security-marked tests: admin auth, webhook HMAC, SSRF, and non-disclosure
	uv run pytest -q -m "security and not fuzz" tests/security
sec-fuzz: ## Schemathesis fuzz of every public API operation — no unexpected server errors
	uv run pytest -q -m fuzz tests/security
mut:       ; uv run mutmut run
mutation: ## Run mutmut over the release program core trio (cost/rate-limit/lease-state)
	uv run mutmut run
mutation-gate: ## Run mutmut + enforce the >=85% kill floor on covered mutants
	uv run mutmut run
	uv run mutmut export-cicd-stats
	uv run python scripts/mutmut_score_gate.py --floor 85
bench: ## pytest-benchmark micro-benchmarks (release program perf)
	uv run pytest -q -m benchmark --benchmark-only tests/perf
load: ## Locust load profile vs a running Pitwall (PITWALL_HOST, default :8080; dry_run writes)
	uv run locust -f tests/load/locustfile.py --headless \
		-u $${LOCUST_USERS:-50} -r $${LOCUST_SPAWN:-5} --run-time $${LOCUST_TIME:-2m} \
		--host $${PITWALL_HOST:-http://127.0.0.1:8080}
load-smoke: ## Hermetic smoke: the locustfile imports + tasks are well-formed
	uv run pytest -q -m slow tests/load
