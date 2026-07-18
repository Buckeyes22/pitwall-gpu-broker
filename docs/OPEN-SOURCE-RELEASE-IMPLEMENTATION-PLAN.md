# Open-Source Release Remediation and Implementation Plan

| Field | Value |
| --- | --- |
| Status | Repository remediation implemented and locally verified; public publication remains blocked on external approvals and hosted-system actions |
| Target | First credible public alpha release |
| Last updated | 2026-07-18 |
| Source | Comprehensive open-source readiness audit |
| Primary audience | Maintainers, security reviewers, release engineers, contributors |
| Governing principle | Do not publish a package, image, release, or public stability claim until every Alpha Blocker gate is satisfied |

## Current execution status

This document is both the implementation plan and the completion record for the
repository work performed on 2026-07-17. `Verified` means the implementation and
its in-repository acceptance evidence pass locally. `External gate` means the
repository-side implementation is complete, but an authorized person or hosted
system must still supply evidence. It does **not** mean the public release is
approved. The authoritative reusable checklist for those remaining actions is
[`docs/release/external-release-gates.md`](release/external-release-gates.md).

| Work items | Repository disposition | Remaining release disposition |
| --- | --- | --- |
| GOV-001 | **Verified.** The project owner selected the neutral `Pitwall` display identity and `pitwall-gpu-broker` distribution/CLI/repository/image slug, accepted the dated search record, removed the provider trademark from product identifiers, and created the public canonical GitHub repository with `main` as its default branch on 2026-07-18. Package metadata and repository terminology checks pass. | The reviewed initial source push and hosted repository controls remain external. PyPI/TestPyPI are deferred; GHCR is an approved first-alpha channel. |
| GOV-002 | **Verified.** The source-provenance inventory and contribution/DCO controls are complete. On 2026-07-18, the project owner attested to sole authorship/ownership, absence of employer/client/contractor/outside-contributor claims, absence of copied proprietary/private source, and authority to publish the inventoried material under Apache-2.0 with the current NOTICE. | Reopen if contrary evidence appears or a future release introduces another rights holder or source category. |
| GOV-003 | **Verified.** A machine-enforced runtime license policy, exact dependency report, NOTICE review material, and transitive-license analysis are complete. On 2026-07-18, the project owner approved the exact graph and the documented LGPL/MPL treatment for both Python artifacts and GHCR images. | Re-run and reapprove if the exact dependency graph, detected expressions, modification status, or distribution form changes. |
| GOV-004, GOV-005 | Added governance, maintainer, support, security, CODEOWNERS, DCO, issue-template, and hosting-control documentation. Workflow policy tests pass. Private Vulnerability Reporting, secret scanning, push protection, Dependabot alerts/security updates, Discussions, signed web commits, immutable action references, protected release environments, an immutable verified-commit `v*` tag ruleset, and 22 GitHub-Actions-bound required checks with review/linear-history/no-force-push/no-deletion protection were enabled and API-verified on GitHub on 2026-07-18. | **External gate:** the owner must add a backup maintainer or explicitly accept the documented single-maintainer risk. Emergency administrator bypass remains enabled until then. |
| SEC-001 through SEC-006 | **Verified.** REST and administrative authorization, stdio-only MCP, inbound HMAC verification, outbound webhook encryption/HMAC/SSRF controls, scoped auth, redaction, body/concurrency/rate limits, dependency scanning, SAST, deterministic secret scanning, and security regression/fuzz suites are implemented. | No repository work remains; hosting security controls are covered by GOV-005's external gate. |
| API-001 through API-004 | **Verified.** Lease mutation/idempotency semantics are atomic, public error/OpenAPI contracts are normalized and compatibility-tested, webhook management/delivery is secured, and REST/MCP/CLI/provider surfaces are reconciled. | No repository work remains. |
| PKG-001 through PKG-004 | **Verified.** The renamed wheel/sdist contain the required package resources and migrations, CLI entry points work outside the checkout, metadata passes Twine, and artifact manifests satisfy the allowlist. | The GitHub Release attaches these verified artifacts. Python registry reservation and publication are deferred. |
| DEP-001 | **Verified.** Required jobs use the frozen lock; dependency audit/license gates pass; scheduled highest and lowest-direct compatibility lanes are defined. | The scheduled GitHub run becomes hosted evidence once the repository exists. |
| SBOM-001 | **Verified.** Source/runtime inventory generation and release attachment automation exist; CycloneDX SBOMs were generated for the package graph and every supported local image. | Published SBOM attachment and attestation are release-job external evidence. |
| IMG-001, IMG-002 | **Verified.** Five non-root, health-checked images and the canonical read-only, capability-dropped Compose stack build and pass readiness, auth-boundary, and graceful-shutdown smoke tests. | No repository work remains. |
| IMG-003 | All five supported image build/scan/promotion jobs exist; local image scans report no fixed HIGH/CRITICAL findings, secrets, or misconfigurations. | **External gate:** GHCR is approved for the first alpha; staging and production push/pull evidence must be completed on the canonical repository. |
| WRK-001 | **Verified scope-removal decision.** The unsupported vLLM worker image/entry point was removed from the alpha surface and the deferral is recorded in the ADR and capability matrix. | A future worker is post-alpha work and carries no current support claim. |
| CI-001 through CI-005 | **Verified.** Hermetic quality/integration/coverage lanes, deterministic secret scanning, honest markers/skips, pinned actions, least permissions, DCO, CodeQL, OpenAPI, release policy, and branch/portable-CI decisions are enforced. | Required-check and branch-rule activation on the canonical host remains external. |
| REL-001, REL-002 | **Verified repository implementation.** A disabled-by-default protected release state machine, immutable candidate validation, version/changelog validation, compatibility checks, and rollback documentation exist. | The protected GHCR/GitHub Release environments, signed tag, and release approval remain external. Deferred Python registries have a separate enable gate. |
| REL-003, REL-004 | Reproducible double builds, manifest inspection, clean installation, checksums, SBOMs, image scans, staging promotion checks, and signing/attestation steps are implemented. | **GitHub-first external gate:** verify GHCR staging/production digests, attached artifacts, signature/provenance, and withdrawal/deprecation procedures. TestPyPI/PyPI rehearsals remain deferred. |
| TST-001 through TST-003 | **Verified.** Wheel and sdist install/migrate/boot outside the checkout; risk coverage and mutation floors pass; concurrency, upgrade/restore, failure, Python, and dependency compatibility matrices are implemented. | Hosted scheduled compatibility results become additional release evidence. |
| TST-004 | Live tests are isolated behind an explicit protected release environment, spend controls, and mandatory non-skipping credentials. | **External gate:** run approved paid RunPod/GPU acceptance and verify cleanup. |
| TST-005 | **Verified.** Executable documentation/user journeys, internal-link validation, artifact smoke, image smoke, and clean-machine contracts are implemented. The canonical source and Discussions links resolve after the reviewed initial source push. | No repository work remains. |
| DAT-001 through DAT-004 | **Verified repository implementation.** Data inventory/privacy posture, encrypted webhook and archive secret handling, bounded retention/archive/purge lifecycle, export/deletion behavior, and redacted logging/backup paths are documented and tested. | **External gate:** privacy reviewer approval and any future hosted-service assessment. |
| OPS-001 through OPS-004 | **Verified.** Immutable/checksummed migrations, concurrent migration locking, upgrade/recovery guidance, real safe backup/restore drill, dependency-aware readiness, shutdown/resource controls, metrics/alerts, incident and support procedures are implemented. | External on-call contacts and hosted alert routing require owner configuration. |
| DOC-001 through DOC-004 | **Verified.** Alpha claims, install/deploy/security/API/operator/privacy/support documentation, terminology scrub, internal links, and external-link automation are updated. Canonical GitHub links now resolve after the reviewed initial source push and Discussions enablement. | No repository work remains. |
| HYG-001 | **Verified.** Ignore/artifact policies, source-distribution allowlists, local-tool exclusions, private-example removal, secret/history review procedures, and clean-artifact checks are implemented. | Final signed clean-clone evidence belongs to the external go/no-go record. |

### Local verification snapshot

- Final all-marker repository suite: 3,348 passed and 32 live/external tests
  skipped. Hermetic coverage lane: 3,231 passed, 20 skipped, 129 deselected; integration
  lane: 116 passed; combined source coverage: 82%, with every risk-specific line
  and branch floor passing.
- Dependency compatibility: the Python 3.12 lowest-direct graph passes 3,231
  tests (31 skipped, 118 deselected) and the highest-compatible affected-surface
  run passes 98 tests. CI uses `uv sync --frozen`/`uv run --frozen`, so these
  lanes cannot silently replace their deliberately selected lock graph.
- Mutation gate: 94.4% (1,444 of 1,530 covered mutants killed), above the 85%
  floor.
- Security: Bandit, Semgrep (200 rules/205 files), pip-audit, license policy,
  deterministic secret review, 46 focused security tests, and all 43 OpenAPI
  fuzz operations pass.
- Artifacts: wheel and sdist are byte-for-byte reproducible with
  `SOURCE_DATE_EPOCH`; Twine/content policy and isolated wheel/sdist CLI,
  migration, onboarding, API readiness, dry-run, and shutdown tests pass. The
  final locally verified SHA-256 values are
  `4a4f7f26b6104d75e4d91b9d77eebae8e9a653c44b65868ad2cca39e3a0dc0c4`
  (wheel) and
  `2b777940da5ca335461c16d52caac8b8a1044993a4e9811e2bb99b06cc3d8961`
  (sdist); published artifacts must be rebuilt by the protected release job
  and verified against that job's own signed provenance.
- Images: all five supported images build non-root; the hardened canonical
  Compose smoke passes; five CycloneDX image SBOMs validate; Trivy reports zero
  fixed HIGH/CRITICAL vulnerabilities, secrets, or misconfigurations.
- Release rehearsal: 17 hermetic release-tier checks pass, eight live-provider
  envelopes correctly skip locally, and the strict audit passes 19/19 checks.
- Documentation: all 66 public-tree Markdown files pass link/anchor checks;
  the canonical source and Discussions links resolve on the public repository.
- Static and policy gates: Ruff (560 files), strict Mypy (201 source files),
  OpenAPI compatibility, Actionlint/workflow policy, repository text/policy
  guards, candidate validation, and `git diff --check` all pass.

### Current release decision

The repository implementation is ready for external review, but publication is
**NO-GO** while any item in `docs/release/external-release-gates.md` is unchecked.
In particular, no local test can substitute for legal/privacy approval, hosted
repository controls, registry ownership, protected publisher environments, a
paid live-provider run, signatures/attestations on the exact published bytes, or
the rollback and credential-revocation drill.

## 1. Purpose

This plan converts the open-source readiness audit into an executable remediation
program. It covers product behavior, security, packaging, containers, deployment,
CI/CD, testing, privacy, operations, legal provenance, governance, community
health, documentation, and release evidence.

This is a release-remediation plan, not a feature roadmap. Until the first public
alpha passes the gates in this document, work in docs/FEATURE-ROADMAP.md should
not displace Alpha Blocker work unless it directly closes one of these tasks.

The repository already has a substantial foundation: Apache-2.0 licensing and a
NOTICE file, contribution and DCO intent, community templates, extensive operator
documentation, strict typing, a large hermetic test suite, integration tests,
CodeQL and dependency automation, optional API authentication and rate limiting,
administrative routes that fail closed, and initial SBOM and dependency-license
artifacts. The work below is intended to make those foundations truthful,
installable, secure by default, reproducible, and supportable.

## 2. Release definitions and priority

| Label | Meaning | Publication rule |
| --- | --- | --- |
| Alpha Blocker | A defect that can expose users, spend money unexpectedly, break the advertised install/deploy path, create legal ambiguity, or make release evidence untrustworthy | Must be complete before any public package, image, or release tag |
| Alpha Required | Necessary for an honest, operable, supportable public alpha but not an immediate containment issue | Must be complete before announcing the alpha |
| GA Required | Required before calling the project stable or production-ready | May be deferred only if the alpha limitation is explicit |
| Post-GA | Improvement that does not invalidate alpha claims | Track separately from this release gate |

The first release should be described as a pre-1.0 alpha. It must not be called
stable, generally available, or production-ready. A feature may be deferred from
the alpha only when its code is disabled or removed from supported surfaces and
all claims, examples, schemas, and deployment manifests are updated consistently.

## 3. Decision gates

These decisions come first because they change package names, interfaces, files,
release automation, and the scope of later testing.

| ID | Decision | Required output | Owner role | Blocks |
| --- | --- | --- | --- | --- |
| DG-01 | Select a legally and operationally available project, distribution, CLI, repository, and image name | Written naming decision plus trademark and package-registry search record | Project lead + legal/IP reviewer | PKG-001, DOC-001, REL-001 |
| DG-02 | Decide whether the RunPod worker is a supported alpha feature or is removed/deferred | Supported-feature matrix and an architecture decision record | Runtime maintainer | WRK-001, IMG-003, TST-004 |
| DG-03 | Select alpha distribution surfaces: source, GitHub Release, PyPI, GHCR, and/or other registries | Release-channel matrix with owners and rollback procedures | Release engineering | REL-001 through REL-004 |
| DG-04 | Define the secure-default posture for REST, MCP, webhook receiver, and administrative interfaces | Threat model and authentication/authorization matrix | Security maintainer | SEC-001, SEC-002, API-003 |
| DG-05 | Decide whether the project operates any hosted service, telemetry collector, documentation analytics, or SaaS control plane | Data-controller and privacy-obligation statement | Project lead + privacy reviewer | DAT-001, DOC-001 |
| DG-06 | Decide whether GitLab CI is supported publicly | Keep-and-port or remove decision | Release engineering | CI-005, DOC-004 |

Decision records should live under docs/decisions/ and include the decision,
alternatives considered, security and compatibility consequences, approvers, and
date. A decision that defers functionality must name the code, configuration,
documentation, and tests that will be removed or marked experimental.

## 4. Critical path and execution phases

    Decision gates and provenance
                |
                v
    Secure defaults + broken API contracts + worker scope
                |
                v
    Installable wheel + hardened images + canonical deployment
                |
                v
    Hermetic CI + artifact inspection + risk-focused tests
                |
                v
    Documentation/community cleanup + release rehearsal
                |
                v
    Test registry -> signed alpha artifacts -> public announcement

| Phase | Objective | Exit condition |
| --- | --- | --- |
| 0. Contain and decide | Prevent accidental publication and settle identity, provenance, feature scope, security posture, and distribution channels | DG-01 through DG-06 resolved; release jobs cannot publish accidentally |
| 1. Repair trust boundaries and contracts | Fix unauthenticated spending paths, SSRF, lease mutation defects, secret exposure, and worker ambiguity | All SEC, API, and selected WRK Alpha Blockers pass focused tests |
| 2. Make artifacts real | Make the installed wheel, images, migrations, and canonical deployment work outside the source tree | Clean wheel install, all supported image builds, migrations, and canonical-stack smoke test pass |
| 3. Make evidence reliable | Make CI hermetic, warning-clean on critical categories, risk-focused, and release-aware | Required checks cannot silently skip or drift; artifacts are inspected and traceable |
| 4. Make the project governable | Resolve legal/community placeholders, document support and privacy, scrub internal residue, and enable repository controls | Governance, security reporting, provenance, and public documentation are approved |
| 5. Rehearse and publish | Exercise the complete release from a clean clone and publish first to non-production channels | Signed evidence bundle approved; go/no-go checklist passes |
| 6. Harden toward GA | Close explicitly deferred stability, compatibility, operational, and support commitments | GA checklist is defined and satisfied in a later release |

## 5. Ownership and tracking conventions

Every work item below needs one directly responsible owner before implementation.
Owner names are intentionally not guessed in this document.

| Owner role | Responsibility |
| --- | --- |
| Project lead | Product scope, naming, governance, final release approval |
| Legal/IP reviewer | Trademark, contribution provenance, licensing, notices |
| Security maintainer | Threat model, auth, SSRF, secrets, vulnerability response |
| API maintainer | REST/MCP contracts, schemas, persistence, compatibility |
| Runtime maintainer | Workers, queues, provider lifecycle, graceful shutdown |
| Release engineering | CI, build provenance, registries, signing, branch controls |
| Operations maintainer | Compose, migrations, backup/restore, observability |
| Documentation maintainer | Public claims, guides, templates, terminology |
| Privacy reviewer | Data inventory, retention, egress, operator obligations |

Tracking issues should use the IDs in this document. Each pull request must link
one or more IDs, state the threat or failure mode addressed, add or update tests,
and attach the verification evidence required by the item.

## 6. Workstream A — identity, legal provenance, and governance

### GOV-001 — Rename and identity clearance

- Priority: Alpha Blocker
- Owner: Project lead + legal/IP reviewer
- Problem: The configured Python distribution name is already used by an
  unrelated active PyPI project. The configured GitHub repository was not
  reachable during the audit, the local checkout has no remote or tags, and
  workflow branch assumptions do not match the local default branch.
- Implementation:
  - Perform trademark, package registry, source forge, domain, social handle, and
    container-registry searches for candidate names.
  - Select separate names only where necessary; document the canonical project
    name, Python distribution, import package, CLI, repository slug, image
    namespace, and documentation domain.
  - Prefer retaining the Python import package only if it creates no confusion
    and the transition is documented. Do not upload under the occupied PyPI name.
  - Update project metadata, entry points, URLs, badges, examples, image
    references, environment-variable prefixes if required, trusted-publisher
    configuration, and all release workflows atomically.
  - Create the canonical repository and define the default branch before enabling
    release automation.
- Expected areas: pyproject.toml, README.md, docs/, .github/, compose files,
  Dockerfiles, package source, shell scripts, badges, and registry settings.
- Dependencies: DG-01, DG-03.
- Acceptance criteria:
  - A dated clearance record identifies searches performed and approval.
  - Distribution and image names are demonstrably available or reserved.
  - A repository-wide search finds no obsolete public identifiers except an
    intentional migration note.
  - Package metadata and every public URL resolve to the selected identity.
  - The default branch is consistent across the repository and hosting settings.
- Verification:

      rg -n "pitwall|buck""eyes22|master|main" .
      uv build
      unzip -p dist/*.whl "*/METADATA" | sed -n "1,80p"

### GOV-002 — Source and contribution provenance review

- Priority: Alpha Blocker
- Owner: Legal/IP reviewer + project lead
- Problem: The visible history is a single squashed initial-release commit and
  repository content contains internal project terminology and planning residue.
  Public release requires evidence that the project has authority to publish all
  code, configuration, documentation, examples, datasets, prompts, and assets.
- Implementation:
  - Inventory all source origins, authors, employer/contractor relationships,
    imported snippets, generated code, model-assisted material, copied
    configuration, sample payloads, and third-party assets.
  - Obtain contributor/employer/contractor publication approvals where required.
  - Verify third-party license compatibility and attribution; remove or rewrite
    material whose provenance cannot be established.
  - Preserve a private provenance ledger without confidential code or secrets.
  - Decide and document the copyright-holder convention behind “The Pitwall
    Authors” or its replacement.
  - Scrub references such as internal plan numbers, PWL/PaddockLink, vo-context,
    HENDRICK evidence, earlier private deployments, and private infrastructure
    unless deliberately retained with public context.
- Expected areas: entire tracked tree, git history, NOTICE, LICENSE, AUTHORS or
  equivalent, docs/, examples/, tests/fixtures/.
- Dependencies: DG-01.
- Acceptance criteria:
  - Every material source category has an owner/origin and publication basis.
  - No known proprietary, confidential, customer, employer, or secret material
    remains.
  - NOTICE and copyright statements match the approved provenance record.
  - Legal/IP approval is recorded as a release artifact.
- Verification:

      git log --all --decorate --stat
      rg -ni "hendrick|paddocklink|vo-context|plan [0-9]+|internal|private deployment" .
      reuse lint

### GOV-003 — Dependency license closure

- Priority: Alpha Required
- Owner: Legal/IP reviewer + release engineering
- Problem: The current legal review flags dependencies including Paramiko under
  LGPL terms and Certifi/TQDM under MPL terms. The SBOM and lock are out of sync,
  so the license analysis is not a reliable release record.
- Implementation:
  - Regenerate dependency and license inventories from the exact frozen release
    environment after dependency updates.
  - Confirm dynamic-linking/distribution obligations for LGPL dependencies and
    file-level obligations for MPL dependencies with counsel or an authorized
    reviewer.
  - Record required notices and source-offer or modification obligations.
  - Add an automated license-policy diff that fails on unknown, denied, or
    newly introduced licenses pending review.
  - Define a process for vendored or patched third-party code before any is added.
- Expected areas: SBOM files, dependency-license reports, NOTICE, CI workflows,
  docs/legal/.
- Dependencies: DEP-001, SBOM-001.
- Acceptance criteria:
  - Legal review uses the same dependency graph as the release artifacts.
  - All obligations are documented and satisfied.
  - CI detects license inventory drift and requires explicit approval.

### GOV-004 — Governance, maintainership, and DCO enforcement

- Priority: Alpha Required
- Owner: Project lead
- Problem: CODEOWNERS has a single ownership point, the repository lacks a
  maintainer/governance and succession policy, and DCO is documented but not
  enforced.
- Implementation:
  - Add GOVERNANCE.md and MAINTAINERS.md with roles, decision process, inactivity
    handling, security and release backups, conflict resolution, and succession.
  - Define support boundaries and response expectations without promising an SLA
    the project cannot sustain.
  - Enforce Signed-off-by for all contributed commits using a required DCO check.
  - Review CODEOWNERS for security-sensitive and release-sensitive paths and add
    backup reviewers.
  - Document when a CLA might be reconsidered; do not add one merely to duplicate
    an enforced DCO.
- Expected areas: repository root, CODEOWNERS, CONTRIBUTING.md, SUPPORT.md,
  .github/.
- Dependencies: none.
- Acceptance criteria:
  - At least two eligible roles cover release and vulnerability-response duties,
    or the single-maintainer risk is explicitly disclosed with a contingency.
  - Unsigned contributions cannot merge.
  - Governance and support documents contain real contacts and processes.

### GOV-005 — Community and hosting controls

- Priority: Alpha Required
- Owner: Project lead + release engineering
- Problem: Community files contain placeholder contacts, issue templates cite
  nonexistent CLI commands, and important repository security and merge settings
  are not represented as verified release evidence.
- Implementation:
  - Replace placeholder security and conduct contacts with monitored channels.
  - Correct issue forms and templates to use commands that exist in the alpha.
  - Enable and record branch protection/rulesets, required reviews and checks,
    linear or documented merge policy, vulnerability reporting, secret scanning,
    push protection, Dependabot alerts, and security updates.
  - Decide whether to enable Discussions and define where support questions go.
  - Add a repository-settings audit script or checklist because these controls
    are not fully versioned in git.
- Expected areas: SECURITY.md, CODE_OF_CONDUCT.md, SUPPORT.md, CONTRIBUTING.md,
  .github/ISSUE_TEMPLATE/, repository settings.
- Dependencies: GOV-004, CI-004.
- Acceptance criteria:
  - All contacts are monitored and tested.
  - Templates contain only valid commands and current links.
  - A dated repository-settings export/checklist is in the release evidence.

## 7. Workstream B — security and trust boundaries

### SEC-001 — Secure-by-default authentication and authorization

- Priority: Alpha Blocker
- Owner: Security maintainer + API maintainer
- Problem: API token authentication is optional, which can leave a
  money-spending control plane and data plane open. MCP has no equivalent network
  authentication. Existing documentation contradicts the administrative
  routes' fail-closed behavior.
- Implementation:
  - Produce a surface-by-surface threat model for REST, MCP stdio, MCP HTTP,
    inbound webhook, outbound webhook, metrics, and administrative operations.
  - Require authentication when any spending or sensitive interface binds beyond
    loopback. Refuse unsafe startup unless an explicit development-only profile
    is selected.
  - Add scopes or roles for read, submit/spend, lease mutation, webhook
    administration, and server administration.
  - Restrict unauthenticated MCP to local stdio. Add authenticated HTTP transport
    or reject non-loopback network binding.
  - Make compose production profiles require secrets rather than silently accept
    blank values.
  - Reconcile SECURITY.md, configuration docs, examples, and OpenAPI security
    declarations with actual behavior.
- Expected areas: settings, middleware/dependencies, REST routes, MCP server,
  compose files, SECURITY.md, operator docs.
- Dependencies: DG-04.
- Acceptance criteria:
  - Default non-loopback startup without credentials fails with an actionable
    error.
  - Spending, lease mutation, and webhook administration require appropriate
    authorization.
  - Loopback development mode is explicit and visibly warns that it is insecure.
  - Positive, negative, scope-boundary, and configuration-matrix tests pass.

### SEC-002 — Outbound webhook SSRF and secret hardening

- Priority: Alpha Blocker
- Owner: Security maintainer + API maintainer
- Problem: Subscription URLs accept arbitrary strings and the dispatcher can
  request localhost, metadata, private, link-local, or otherwise sensitive
  endpoints. Redirect and DNS-rebinding behavior is uncontrolled. Subscription
  management is open under default data-plane settings, HMAC secrets are stored
  in plaintext, payload serialization is not valid JSON, and timeout retry
  behavior contradicts its contract.
- Implementation:
  - Require authorization and an administrative webhook-management scope for
    subscription creation, listing, rotation, deactivation, and deletion.
  - Parse and normalize URLs, require HTTPS by default, reject user info and
    ambiguous host forms, and restrict ports under a documented policy.
  - Resolve all A/AAAA answers and reject loopback, RFC1918, carrier-grade NAT,
    link-local, multicast, reserved, unspecified, and cloud metadata ranges.
  - Revalidate the connected peer and each redirect target to mitigate DNS
    rebinding and open redirects; disable redirects unless explicitly needed.
  - Serialize a canonical JSON byte sequence, sign those exact bytes, and set
    matching content type and length.
  - Implement an explicit delivery state machine with bounded attempts,
    exponential backoff and jitter, terminal failure state, idempotency/delivery
    ID, and consistent timeout handling.
  - Encrypt subscription secrets at rest with key versioning and rotation; return
    secret material only at creation/rotation. Redact URLs and response bodies in
    errors where they may contain secrets.
  - Add full management lifecycle endpoints and audit events.
- Expected areas: webhook schemas, routes, repository/model, dispatcher,
  migrations, settings, security docs.
- Dependencies: SEC-001, DAT-002.
- Acceptance criteria:
  - Tests cover IPv4/IPv6 private addresses, encoded/decimal hosts, localhost,
    metadata targets, redirects, DNS answer changes, mixed public/private DNS,
    connection peer mismatch, and allowed public HTTPS endpoints.
  - Request bodies parse as JSON and signature verification uses the transmitted
    bytes.
  - Timeout, retry, terminal failure, rotation, and deactivation tests pass.
  - No plaintext webhook secret is readable from normal database queries or list
    responses after migration.

### SEC-003 — Inbound webhook protection

- Priority: Alpha Blocker if exposed; otherwise Alpha Required
- Owner: Security maintainer
- Problem: Inbound HMAC validation is optional/off by default and the receiver
  lacks an explicit request-size and abuse-control posture.
- Implementation:
  - Require HMAC configuration for non-loopback binding and fail startup if absent.
  - Enforce strict body-size, content-type, timestamp-window, and rate limits
    before expensive parsing.
  - Define replay semantics. If full nonce deduplication is not implemented,
    document the timestamp window accurately and do not call it complete replay
    prevention.
  - Forward required configuration through the canonical deployment.
  - Make verification constant-time and rotate keys without downtime.
- Acceptance criteria:
  - Non-loopback unauthenticated startup is rejected.
  - Oversized, stale, malformed, replayed where deduplication is promised, and
    rate-limited requests have stable error contracts and tests.

### SEC-004 — Dependency vulnerability closure

- Priority: Alpha Blocker
- Owner: Security maintainer + release engineering
- Problem: The frozen environment contains MCP 1.27.2, reported vulnerable to
  CVE-2026-59950 and fixed in 1.28.1. The vulnerable transport may not be used,
  but the release gate currently fails.
- Implementation:
  - Upgrade to a non-vulnerable compatible version and run all MCP contract tests.
  - If a temporary exception is unavoidable, create a time-bounded VEX record
    that proves the affected code is unreachable, names an owner, and has an
    expiry. A permanent ignore is not acceptable.
  - Add dependency audit to every release candidate and a scheduled workflow.
- Expected areas: pyproject.toml, uv.lock, audit policy/VEX, MCP tests, CI.
- Acceptance criteria:
  - The frozen release environment passes the vulnerability gate or has an
    approved, unexpired VEX with evidence.

### SEC-005 — Secret handling and log redaction

- Priority: Alpha Blocker
- Owner: Security maintainer + runtime maintainer
- Problem: The worker logs the complete Redis URL, database/webhook secrets have
  weak storage or transport assumptions, and operational tooling can expose a
  database URL in process arguments.
- Implementation:
  - Introduce centralized structured redaction for URLs, headers, tokens, HMAC
    values, payloads, error messages, and tracing attributes.
  - Never print a complete Redis or database URL. Log only a safe host identifier
    when operationally necessary.
  - Move secret construction and transfer out of command-line source strings and
    process arguments.
  - Document secret sources, minimum privileges, rotation, revocation, and
    supported secret-manager integrations.
  - Add canary-secret tests against captured logs and traces.
- Dependencies: DAT-002, OPS-002.
- Acceptance criteria:
  - Repository searches and runtime capture show no secret-bearing connection
    strings or authorization values.
  - Rotation procedures are tested without redeploying unrelated components.

### SEC-006 — Abuse resistance and security automation

- Priority: Alpha Required
- Owner: Security maintainer + release engineering
- Problem: Rate limiting and body-size protection are not consistently applied,
  action dependencies are mutable, and hosted security controls are unverified.
- Implementation:
  - Apply bounded request bodies, timeouts, concurrency controls, and rate limits
    at every network ingress with documented defaults.
  - Pin every third-party GitHub Action to an immutable commit SHA and annotate
    the human-readable version.
  - Grant least-privilege workflow permissions and isolate pull-request code from
    publishing credentials.
  - Keep Bandit, Semgrep, CodeQL, secret detection, dependency audit, and image
    scanning as required checks with explicit baselines and expiry rules.
- Acceptance criteria:
  - A workflow-permissions review finds no unnecessary write permissions.
  - Untrusted pull requests cannot access release secrets or publish artifacts.
  - Abuse-limit tests and SAST gates pass from a clean checkout.

## 8. Workstream C — API, persistence, and product contracts

### API-001 — Correct atomic lease renewal and patch semantics

- Priority: Alpha Blocker
- Owner: API maintainer
- Problem: REST renewal ignores its body and returns the existing lease; patch
  accepts fields that are ignored; auto-teardown is a no-op; renewal-policy
  handling attempts to write a value into the lease state field and can violate
  the database constraint. MCP renewal behaves differently from REST.
- Implementation:
  - Define one lease-mutation domain service used by REST and MCP.
  - Implement repository methods for renewal and supported patches in a database
    transaction with row locking or optimistic concurrency.
  - Bound extension values, expiry horizons, and state transitions. Decide
    whether renewal is additive from current expiry or from now and document it.
  - Persist renewal policy and auto-teardown in their actual columns or remove
    them from the public schema until implemented.
  - Reject unsupported/immutable patch fields with a stable 4xx response instead
    of silently ignoring them.
  - Add idempotency semantics, audit records, conflict behavior, and authorization.
  - Make REST, MCP, CLI, OpenAPI, and documentation use the same contract.
- Expected areas: lease schemas/routes/service/repository, MCP tools, migrations,
  tests, API docs.
- Dependencies: SEC-001.
- Acceptance criteria:
  - Real-Postgres tests prove expiry, policy, and auto-teardown persistence.
  - Concurrent renewal tests cannot lose updates or create invalid states.
  - Unsupported mutations are rejected; no accepted input is ignored.
  - REST and MCP conformance tests produce equivalent state changes.

### API-002 — Public contract cleanup and compatibility policy

- Priority: Alpha Required
- Owner: API maintainer
- Problem: OpenAPI emits duplicate operation IDs for proxy methods, error
  responses are incomplete, some schema-conformance operations are skipped, and
  pre-1.0 compatibility/deprecation expectations are unclear.
- Implementation:
  - Assign deterministic unique operation IDs and make OpenAPI generation
    warning-free.
  - Declare standard authentication, validation, conflict, rate-limit, provider,
    and server error schemas and headers.
  - Remove unjustified Schemathesis skips; document any unavoidable exclusion
    with an owner and expiry.
  - Publish a pre-1.0 compatibility, versioning, deprecation, and removal policy.
  - Add schema diffing to release CI and require approval for breaking changes.
  - Clarify intentionally unsupported provider operations, such as Together
    lease functionality, without representing them as available.
- Acceptance criteria:
  - OpenAPI has no duplicate IDs and passes validation.
  - Contract tests cover declared errors and supported operations.
  - Breaking schema changes are detected between the candidate and previous tag.

### API-003 — Webhook management and delivery contract

- Priority: Alpha Blocker
- Owner: API maintainer + security maintainer
- Problem: Subscription lifecycle, authentication, payload format, failure
  behavior, and secret exposure are incomplete or inconsistent.
- Implementation:
  - Complete create, list-safe-metadata, rotate-secret, deactivate/reactivate, and
    delete operations.
  - Define event versioning, delivery IDs, timestamps, signing headers, retry
    policy, retention, and receiver idempotency expectations.
  - Keep secret values write-only and redact sensitive destination components.
  - Publish an example verifier and golden payload/signature fixtures.
- Dependencies: SEC-001, SEC-002, DAT-003.
- Acceptance criteria:
  - Public lifecycle and delivery behavior is represented in OpenAPI and tests.
  - Golden fixtures verify across at least two independent implementations or
    one implementation plus a simple reference script.

### API-004 — Cross-surface parity and dead-code cleanup

- Priority: Alpha Required
- Owner: API maintainer
- Problem: REST, MCP, CLI, and docs disagree in behavior. MCP registry comments
  describe stubs and unused helper stubs remain, while public surfaces imply more
  support than exists.
- Implementation:
  - Build a capability matrix for every supported operation and provider.
  - Either implement parity or explicitly label a surface/provider combination
    unsupported with stable errors.
  - Remove dead stubs and stale registry descriptions.
  - Generate or test command/tool documentation from executable interfaces where
    practical.
- Acceptance criteria:
  - The capability matrix matches executable behavior.
  - A clean-tree search finds no stale “stub” claims for implemented paths.

## 9. Workstream D — package and source artifacts

### PKG-001 — Package name and metadata migration

- Priority: Alpha Blocker
- Owner: Release engineering
- Problem: The current PyPI distribution name cannot be safely used and metadata
  points at a repository that was unavailable during the audit.
- Implementation:
  - Apply DG-01 to project.name, console entry points, metadata URLs, README
    install commands, classifiers, trusted publishing, and release jobs.
  - Validate Python and OS classifiers against the tested compatibility matrix.
  - Reserve the new name and publish only a non-production smoke artifact after
    all release gates permit it.
- Dependencies: DG-01, GOV-001.
- Acceptance criteria:
  - Metadata URLs resolve and names are consistent.
  - No upload is attempted to the unrelated occupied project.

### PKG-002 — Ship migrations as package resources

- Priority: Alpha Blocker
- Owner: API maintainer + release engineering
- Problem: The wheel omits db/migrations and installed code searches for that
  source-tree directory. A fresh wheel installation therefore cannot initialize
  or migrate a database.
- Implementation:
  - Move SQL migrations beneath the import package or a dedicated packaged data
    package.
  - Load them with importlib.resources rather than repository-relative paths.
  - Explicitly include migration data in wheel and sdist configuration.
  - Preserve deterministic ordering, checksums, and migration identity.
  - Test status, initialize, and upgrade commands in a temporary directory with
    only the installed wheel and a real Postgres service.
- Expected areas: src package resources, migration loader, pyproject.toml,
  migration tests, release workflow.
- Acceptance criteria:
  - Wheel contents include every required migration and no duplicate source.
  - Migration operations succeed after installing the wheel outside the checkout.
  - Missing/corrupt resource errors are explicit and tested.

### PKG-003 — Stabilize the CLI entry contract

- Priority: Alpha Required
- Owner: API maintainer
- Problem: the CLI exits with an unknown-command error for --help, has no
  --version, and no-argument behavior is not a usable public entry point.
- Implementation:
  - Make root help return zero, add a version sourced from installed package
    metadata, and define intentional no-argument behavior.
  - Standardize exit codes, stderr/stdout use, error rendering, and shell-safe
    examples.
  - Remove nonexistent commands from issue templates and docs.
  - Fix coroutine-not-awaited warnings in CLI error tests.
- Acceptance criteria:
  - Root and subcommand help, version, invalid-command, and no-argument snapshots
    pass from a wheel installation.
  - Public docs contain only commands proven by executable documentation tests.

### PKG-004 — Constrain and inspect wheel and sdist contents

- Priority: Alpha Blocker
- Owner: Release engineering
- Problem: A local sdist captured hundreds of repository files, including
  .remember logs, an untracked audit evidence file, secret-scan baselines,
  workflows, and local configurations. Build inclusion lacks a deliberate
  allowlist, making local releases unsafe.
- Implementation:
  - Define explicit wheel and sdist include/exclude rules. Include only source,
    packaged migrations, essential type information, README, license/notices,
    and intentionally shipped documentation.
  - Exclude VCS state, agent memory, local evidence, credentials/baselines,
    coverage, caches, test artifacts, internal CI, and operator-local files.
  - Require a clean tree, including no unexpected untracked files, before release.
  - Always build releases in a fresh checkout and inspect archive manifests
    against a versioned allowlist/denylist policy.
  - Run metadata validation and install both artifacts in isolated environments.
- Expected areas: pyproject.toml, .gitignore, build/release scripts, CI.
- Acceptance criteria:
  - Artifact-content tests fail on known private/local filename patterns.
  - Wheel and sdist install and import successfully.
  - LICENSE, NOTICE, py.typed, and migration resources are present.
  - A dirty or untracked release workspace cannot publish.

### DEP-001 — Frozen and tested dependency policy

- Priority: Alpha Required
- Owner: Release engineering
- Problem: Some CI and image paths resolve live dependencies instead of using the
  lock, and base/runtime dependency assumptions differ between services.
- Implementation:
  - Use uv sync --frozen or equivalent exact resolution for CI and release.
  - Define how runtime applications consume lock-derived dependencies.
  - Test the declared minimum Python version and each supported Python version.
  - Add a scheduled “latest compatible dependencies” job without weakening the
    frozen release path.
  - Define lock update ownership, vulnerability response, and review rules.
- Acceptance criteria:
  - The same commit resolves the same application dependency graph in CI.
  - Unsupported Python/OS claims are removed from metadata.

### SBOM-001 — Reproducible SBOM and artifact inventory

- Priority: Alpha Required
- Owner: Release engineering + legal/IP reviewer
- Problem: The committed SBOM is stale relative to the lock and its description
  of optional extras does not match its contents.
- Implementation:
  - Generate CycloneDX or SPDX SBOMs from the exact installed wheel and every
    release image after frozen dependency installation.
  - Validate component versions against the lock and image filesystem.
  - Attach SBOMs to release artifacts and attestations; do not treat a manually
    committed timestamped file as authoritative.
  - Automate SBOM diffs, schema validation, and license review input.
- Dependencies: DEP-001, IMG-001, REL-003.
- Acceptance criteria:
  - Versions match released artifacts and the lock where applicable.
  - Every published artifact has an associated validated SBOM.

## 10. Workstream E — containers, deployment, and worker runtime

### IMG-001 — Repair and harden service images

- Priority: Alpha Blocker
- Owner: Release engineering + runtime maintainer
- Problem: Five primary service Dockerfiles fail while installing the package
  because README.md is absent from their build stage. Images use floating bases,
  non-frozen installs, root users, and lack consistent health, provenance, and
  metadata. There is no .dockerignore.
- Implementation:
  - Use a shared, tested multi-stage build pattern or deliberately independent
    Dockerfiles with parity tests.
  - Copy all build metadata before installing, consume locked dependencies, and
    verify the installed package rather than depending on source-tree layout.
  - Pin base images by digest with an automated update policy.
  - Create an unprivileged runtime user, minimal writable directories, read-only
    filesystem compatibility where possible, and explicit signal handling.
  - Add OCI labels, health checks, deterministic entry points, and .dockerignore.
  - Scan images and generate SBOM, provenance, and signatures.
  - Review the worker image's --no-deps assumption and make every runtime
    dependency explicit.
- Expected areas: all Dockerfiles, .dockerignore, pyproject/lock, workflows,
  container tests.
- Acceptance criteria:
  - Every supported image builds from a clean checkout with no network resolution
    beyond the declared frozen process.
  - Containers run as non-root and pass health and shutdown tests.
  - Critical/high image findings are fixed or have approved expiring VEX records.

### IMG-002 — Canonical secure deployment stack

- Priority: Alpha Blocker
- Owner: Operations maintainer
- Problem: Production compose maps and health-checks port 8000 while the API image
  listens on 8080, omits core services, accepts weak database defaults, and
  references an image that is not built. The fuller compose publishes services
  that bind only to container loopback and omits required environment for MCP and
  security controls.
- Implementation:
  - Declare one canonical supported compose topology with explicit profiles for
    required and optional components.
  - Include API, database, Redis, reconciler, supported worker, webhook, exporter,
    and MCP only when each is part of the alpha support matrix.
  - Align listen addresses, internal/external ports, health checks, dependencies,
    migrations, readiness, and shutdown behavior.
  - Bind services to 0.0.0.0 inside a container when they must be published, while
    binding host ports to loopback by default.
  - Forward required API auth, rate-limit, webhook HMAC, provider, database, and
    Redis configuration. Require nonblank secrets for production profiles.
  - Document Redis/Postgres authentication and TLS choices, persistent storage,
    resource limits, and upgrade order.
  - Use project-scoped names and remove hard-coded container_name values so
    sibling clones and forks can run concurrently.
  - Add a one-shot migration service and ensure application services wait for
    successful migration plus dependency readiness.
- Expected areas: docker-compose*.yml, env examples, operator docs, health code,
  smoke tests.
- Dependencies: IMG-001, PKG-002, SEC-001, SEC-003, WRK-001.
- Acceptance criteria:
  - A clean clone can boot the canonical stack with one documented command.
  - All published endpoints are reachable only at intended host interfaces.
  - Health/readiness becomes green after migrations and red on dependency loss.
  - Two uniquely named stack instances can run without name/volume collisions.
  - An end-to-end submit/status/result or supported equivalent journey succeeds.

### IMG-003 — Build and publish every supported image

- Priority: Alpha Blocker
- Owner: Release engineering
- Problem: Existing automation builds only the worker-vLLM image; it does not
  build/publish the API stack image. Worker workflow triggers, fixed values, and
  manual VLLM input disagree, and multi-architecture GPU support is unproven.
- Implementation:
  - Define the exact image matrix and tags for each supported service.
  - Build all images on pull requests without publishing; publish immutable
    version and digest references only from an approved release.
  - Remove ignored workflow inputs or wire them through and test them.
  - Align tag triggers with the release version format.
  - Do not claim multi-architecture GPU support without hardware validation;
    publish only tested platforms.
  - Update compose to use pinned released digests for production examples.
- Dependencies: DG-02, DG-03, IMG-001, REL-001.
- Acceptance criteria:
  - The image matrix is complete and every referenced image exists.
  - Manual inputs change the build they claim to change.
  - A release manifest maps source commit, version, platform, and digest.

### WRK-001 — Implement or remove the advertised worker

- Priority: Alpha Blocker
- Owner: Runtime maintainer
- Problem: The image entrypoint uses --capability while the module accepts
  --type. With accepted arguments the module prints configuration and exits
  rather than consuming Arq work. The module has zero measured test coverage and
  logs the complete Redis URL.
- Implementation option A — support it:
  - Define a worker protocol and queue contract, then implement durable Arq
    consumption, acknowledgement, retries, cancellation, timeouts, heartbeats,
    result/error persistence, lease ownership, and graceful shutdown.
  - Align entrypoint arguments with the CLI and validate the configured
    capability/model.
  - Explicitly install and lock runtime and GPU dependencies.
  - Redact connection information and emit structured metrics.
  - Test queue behavior locally and run an actual GPU/provider acceptance test
    for each claimed configuration.
- Implementation option B — defer it:
  - Remove its image from release workflows and canonical compose.
  - Remove or clearly mark its commands, provider capabilities, docs, examples,
    metrics, and schemas as experimental/unavailable.
  - Keep no execution path that appears healthy while immediately exiting.
- Dependencies: DG-02, SEC-005.
- Acceptance criteria:
  - Supported option: entrypoint stays healthy, consumes a real test job, records
    its result, survives transient Redis failure, and shuts down without losing
    acknowledged state.
  - Deferred option: the public alpha makes no claim that this worker is usable
    and no default deployment starts it.

## 11. Workstream F — CI, release automation, and supply chain

### CI-001 — Restore a clean required quality gate

- Priority: Alpha Blocker
- Owner: Release engineering
- Problem: Ruff lint and strict mypy pass, but formatting currently fails on 11
  source files. Tests emit 53 warnings, including unawaited coroutines, duplicate
  OpenAPI IDs, incorrectly marked async tests, datetime and websocket
  deprecations, and Hypothesis decimal warnings.
- Implementation:
  - Apply formatting without mixing unrelated behavior changes.
  - Fix all “coroutine was never awaited” and duplicate-operation warnings before
    release.
  - Remove incorrect async markers and migrate deprecated APIs.
  - Configure CI to fail on high-signal warning classes while maintaining an
    owned, expiring list for low-risk upstream warnings.
  - Keep lint, format, strict typing, and tests as separate diagnosable checks.
- Acceptance criteria:
  - Required checks pass from a clean frozen environment.
  - No resource, unawaited-coroutine, duplicate-operation, or project-owned
    deprecation warnings remain.

### CI-002 — Repair deterministic secret detection

- Priority: Alpha Blocker
- Owner: Security maintainer + release engineering
- Problem: CI scans a different file scope than the baseline. Rewriting the
  baseline removes test entries and causes large drift. Two entries have
  unresolved is_secret values.
- Implementation:
  - Define one canonical scan command and exclusion policy shared by local hooks
    and CI.
  - Recreate and audit the baseline using that exact scope.
  - Resolve every undecided entry as a rotated real secret or documented false
    positive; never commit live credentials.
  - Make baseline drift fail only when produced by the canonical command.
  - Add tests that exercise representative test credentials without teaching
    the scanner to ignore broad secret classes.
- Acceptance criteria:
  - Two consecutive scans are byte-stable.
  - No baseline item has null/undecided status.
  - A planted canary secret causes the check to fail.

### CI-003 — Make test tiers honest

- Priority: Alpha Blocker
- Owner: Release engineering + test owners
- Problem: Release readiness can report success while live checks are skipped;
  mutation testing is nonblocking despite documentation calling it
  authoritative, and no documented nightly enforcement exists.
- Implementation:
  - Separate hermetic, preflight, integration, built-artifact, live-provider, and
    destructive tests into explicitly named jobs.
  - A required gate must fail rather than skip when its credentials or
    prerequisites are expected for that release class.
  - Define which tests gate pull requests, release candidates, alpha, and GA.
  - Keep mutation scope focused on cost, rate-limit, and lease-state logic per
    project preference; either enforce the threshold in a scheduled/release gate
    or change documentation to state it is advisory.
  - Store machine-readable summaries with pass/fail/skip counts and reasons.
- Acceptance criteria:
  - “All GA readiness passed” is impossible when any GA-required test skipped.
  - Release evidence distinguishes not-run, skipped, failed, and passed.
  - Mutation policy and automation agree.

### CI-004 — Harden workflow dependencies and permissions

- Priority: Alpha Required
- Owner: Release engineering + security maintainer
- Problem: Workflows use mutable action tags and publication workflows need
  stronger least-privilege and concurrency boundaries.
- Implementation:
  - Pin third-party actions to audited SHAs.
  - Declare job-level permissions, environment protections, concurrency groups,
    and trusted-publisher boundaries.
  - Prevent duplicate runs and ensure pull-request workflows cannot gain release
    identity tokens.
  - Add scheduled dependency, CodeQL, secret, image, and license scans.
- Acceptance criteria:
  - Workflow policy lint passes and all external actions are immutable.
  - Publishing requires protected-environment approval where configured.

### CI-005 — Portable CI and branch consistency

- Priority: Alpha Required
- Owner: Release engineering
- Problem: Local/default branch naming conflicts with workflow targets, and
  GitLab CI contains private-runner assumptions that public contributors cannot
  reproduce.
- Implementation:
  - Standardize the default branch and update triggers, badges, rulesets, and docs.
  - Apply DG-06: port GitLab jobs to public runners with documented requirements
    or remove the unsupported configuration.
  - Ensure destructive user-journey tests use unique compose projects and volumes.
  - Standardize scripts on uv run or the resolved environment interpreter rather
    than assuming python is on PATH.
- Acceptance criteria:
  - A fork can run all documented public CI without private infrastructure.
  - Sibling checkouts do not collide.

### REL-001 — Single, gated release state machine

- Priority: Alpha Blocker
- Owner: Release engineering
- Problem: Release automation triggers on both tags and release publication,
  risking duplicate uploads, and publication is not causally dependent on the
  complete readiness suite.
- Implementation:
  - Choose one immutable release trigger.
  - Model build, verify, approve, publish-to-test, smoke, publish-production,
    create-release, and announce as ordered states.
  - Build once and promote the exact verified artifacts; do not rebuild between
    test and production publication.
  - Require successful Alpha Blocker/Required gates and protected approval.
  - Add concurrency and idempotency so reruns cannot upload different bytes for
    the same version.
- Dependencies: DG-03, CI-003, CI-004.
- Acceptance criteria:
  - One source event can create at most one production release.
  - A failed or skipped required job prevents publication.
  - Promoted artifact hashes match the verified candidate hashes.

### REL-002 — Version, changelog, and compatibility validation

- Priority: Alpha Required
- Owner: Release engineering + documentation maintainer
- Problem: Automation does not consistently validate tag/version/changelog
  agreement, release date, or compatibility implications.
- Implementation:
  - Define one version source and the allowed tag format.
  - Verify tag, package version, image tag, documentation, and changelog match.
  - Require a dated changelog entry with security/upgrade/breaking-change notes.
  - Run an API/schema compatibility diff against the previous release.
  - Document pre-1.0 SemVer behavior and supported-version/backport policy.
- Acceptance criteria:
  - Mismatched, reused, or undocumented versions cannot build a release.

### REL-003 — Artifact validation, signing, and provenance

- Priority: Alpha Required
- Owner: Release engineering
- Problem: Release jobs lack complete metadata checks, clean-install proof,
  archive inspection, checksums, image scanning, signatures, and provenance.
- Implementation:
  - Run twine check or the equivalent metadata validator.
  - Inspect artifact manifests and install wheel/sdist into isolated environments.
  - Generate SHA-256 checksums, SBOMs, signed attestations, and SLSA-compatible
    provenance for packages and images.
  - Sign artifacts and images with an identity tied to the protected release job.
  - Attempt reproducible double builds with SOURCE_DATE_EPOCH; document and
    minimize any unavoidable nondeterminism.
- Dependencies: PKG-002, PKG-004, IMG-003, SBOM-001.
- Acceptance criteria:
  - Consumers can verify checksum, signature, source commit, builder identity,
    dependency inventory, and image digest.
  - Artifact content and clean-install tests pass before signing.

### REL-004 — Registry publication and rollback

- Priority: Alpha Required for GHCR; deferred for PyPI/TestPyPI
- Owner: Release engineering + operations maintainer
- Problem: The first-alpha GHCR channel needs a proven staging rehearsal and
  documented deprecation process; a future PyPI channel will need the equivalent
  TestPyPI and yank procedures.
- Implementation:
  - Publish the exact image candidate to the staging GHCR namespace before
    production promotion. Before enabling PyPI, publish the exact package
    candidate to TestPyPI using an isolated trusted publisher.
  - Install and deploy by immutable version/digest from those channels.
  - Exercise yank/deprecation, compromised-key, bad-image, and announcement
    correction procedures.
  - Confirm that deleting/replacing immutable release artifacts is not the normal
    rollback mechanism; publish fixed versions instead.
- Acceptance criteria:
  - A clean environment pulls and operates the exact staging GHCR digests; when
    Python publication is enabled, it also installs from TestPyPI.
  - Rollback and key-compromise drills have recorded results.

## 12. Workstream G — testing and verification depth

### TST-001 — Installed-artifact and migration end-to-end tests

- Priority: Alpha Blocker
- Owner: Release engineering + API maintainer
- Problem: Source-tree tests do not detect the missing wheel migrations or CLI
  entry defects.
- Implementation:
  - Build artifacts in a clean checkout.
  - Create an isolated environment outside the repository and install only the
    candidate wheel, then candidate sdist.
  - Run import, CLI help/version, configuration validation, database initialize,
    migrate, status, API boot, and a minimal request flow.
  - Assert that no repository-relative file is required at runtime.
- Acceptance criteria:
  - Both artifacts pass with the checkout unavailable or renamed.

### TST-002 — Risk-based coverage and mutation floors

- Priority: Alpha Required
- Owner: Test owners + security maintainer
- Problem: Global 81% coverage masks worker at 0%, webhook dispatcher at 23%,
  retention at 42%, webhook receiver at 52%, and R2 cleanup at 19%.
- Implementation:
  - Set per-module or per-risk-domain branch coverage floors for spending,
    authorization, webhook, lease, retention/deletion, cleanup, and worker code.
  - Add mutation thresholds for the intentionally scoped cost, rate-limit, and
    lease-state modules.
  - Require tests for every fixed audit defect and regression ID.
  - Do not chase a global number at the expense of boundary and failure testing.
- Acceptance criteria:
  - Critical state transitions, security rejections, retry branches, deletion,
    and failure recovery have direct assertions.
  - Coverage cannot be raised solely by excluding risky modules.

### TST-003 — Compatibility and failure matrix

- Priority: Alpha Required
- Owner: Release engineering + operations maintainer
- Problem: CI covers Python 3.12 only and lacks minimum/latest dependency, OS,
  production upgrade/restore, and failure-injection coverage.
- Implementation:
  - Test every declared Python and supported OS combination or narrow metadata.
  - Test frozen dependencies on every required job and latest-compatible
    dependencies on schedule.
  - Exercise Postgres/Redis unavailable, network timeout, provider error,
    duplicate delivery, restart, SIGTERM, and partial migration scenarios.
  - Test backup/restore and supported upgrade paths on release candidates.
- Acceptance criteria:
  - Metadata claims equal the tested matrix.
  - Failure tests demonstrate bounded retry and recoverable state.

### TST-004 — Provider and GPU live acceptance

- Priority: Alpha Blocker only for features claimed in the alpha
- Owner: Runtime maintainer
- Problem: No release evidence covers live RunPod/GPU lifecycle or the built
  worker image.
- Implementation:
  - Use isolated credentials, spend caps, timeouts, and guaranteed teardown.
  - Test provision, readiness, work execution, renewal, expiry, cancellation,
    teardown, reconciliation, and orphan cleanup.
  - Run the released image digest, not source code.
  - Record cost and resource cleanup evidence without leaking identifiers.
- Dependencies: DG-02, WRK-001, IMG-003.
- Acceptance criteria:
  - Every advertised live-provider path has a passing, non-skipped release result.
  - Teardown verification proves no test resources remain.

### TST-005 — Documentation and clean-machine journeys

- Priority: Alpha Required
- Owner: Documentation maintainer + test owners
- Problem: README and issue-template commands drift from executable behavior, and
  user journeys have not been proven from public artifacts on a clean machine.
- Implementation:
  - Turn quickstart and operator commands into executable documentation tests.
  - Exercise source contributor setup separately from consumer package/image use.
  - Check internal and external links, example configuration, and shell snippets.
  - Isolate compose projects, credentials, ports, and volumes per run.
- Acceptance criteria:
  - A new user can complete each supported journey without private context.
  - No documented command is nonexistent, destructive without warning, or
    dependent on the source checkout unless identified as a contributor command.

## 13. Workstream H — privacy, data lifecycle, and observability

### DAT-001 — Data inventory and privacy posture

- Priority: Alpha Required
- Owner: Privacy reviewer + security maintainer
- Problem: The system can retain workload input/result/error, raw webhook
  payloads, destinations, credentials, and trace metadata without a unified data
  classification or operator-facing privacy model.
- Implementation:
  - Inventory each collected field, purpose, source, destination, storage,
    retention, access role, egress, backup behavior, and deletion path.
  - Classify secrets, credentials, personal data, customer payloads, operational
    metadata, and public data.
  - Document that Langfuse is opt-in/off by default and exactly which metadata and
    error fields it can export.
  - Apply DG-05. If there is no hosted service, provide an operator data-handling
    guide. If the project hosts telemetry/SaaS/analytics, obtain appropriate
    privacy policy, terms, DPA, cookie/analytics, and data-controller review.
- Acceptance criteria:
  - Every sensitive field has an owner and lifecycle.
  - No undocumented network egress occurs in default configuration.

### DAT-002 — Encryption and secret storage

- Priority: Alpha Blocker for secrets; Alpha Required for broader data
- Owner: Security maintainer + operations maintainer
- Problem: Webhook secrets are plaintext and database/cache transport and
  at-rest assumptions are not fully specified.
- Implementation:
  - Encrypt reversible integration secrets with envelope encryption and key
    versioning; hash values that never need recovery.
  - Define rotation, revocation, backup, restore, and migration behavior.
  - Document TLS/auth requirements for remote Postgres and Redis and encryption
    expectations for disks, object storage, archives, and backups.
  - Restrict database roles and file permissions to least privilege.
- Acceptance criteria:
  - Database dumps and normal APIs do not expose usable integration secrets.
  - Rotation and restore tests preserve availability and confidentiality.

### DAT-003 — Real retention, purge, export, and deletion

- Priority: Alpha Required
- Owner: Operations maintainer + privacy reviewer
- Problem: The current retention operation copies old workload rows to JSONL but
  does not delete them, duplicating sensitive data. An optional archive directory
  means retention is not enforced, and archive permissions/encryption/lifecycle
  are unspecified.
- Implementation:
  - Separate archive and purge policies. Make each idempotent, transactional where
    possible, auditable, and explicit about related rows and object storage.
  - Delete or anonymize source records after verified archive only when policy
    requires it; avoid duplicate indefinite retention.
  - Define archive filesystem permissions, encryption, checksums, destination,
    failure recovery, and lifecycle deletion.
  - Provide operator commands/jobs for retention, deletion, and export with dry
    run and bounded batches.
  - Cover webhook records, traces, audit logs, R2 objects, backups, and derived
    records, not only workloads.
- Acceptance criteria:
  - Integration tests prove records and associated objects are retained, purged,
    or exported exactly according to configured policy.
  - Partial failures do not silently duplicate or delete unverified data.

### DAT-004 — Logging, tracing, and backup redaction

- Priority: Alpha Required
- Owner: Security maintainer + operations maintainer
- Problem: Payloads, URLs, provider errors, connection strings, and trace
  attributes can contain sensitive material.
- Implementation:
  - Create an allowlist-based telemetry schema and centralized redaction.
  - Cap error and payload sizes and avoid raw workload data by default.
  - Add automated canary-secret scans of logs, traces, exports, and backup
    diagnostics.
  - Document debug-mode risks and make sensitive logging require explicit,
    time-bounded activation.
- Acceptance criteria:
  - Default telemetry contains operational identifiers but no raw payloads or
    credentials.

## 14. Workstream I — operations, migration, and recovery

### OPS-001 — Upgrade and migration safety

- Priority: Alpha Required
- Owner: Operations maintainer + API maintainer
- Problem: Migrations are forward-only and installed delivery is broken.
  Operators need a safe upgrade and supported downgrade/restore position.
- Implementation:
  - Fix packaged migrations through PKG-002.
  - Add preflight checks, migration locks, checksums, status, backup-before-upgrade
    guidance, and clear failure recovery.
  - Define whether downgrade means down migrations or restore-from-backup. For
    alpha, a tested restore strategy is acceptable if clearly documented.
  - Test upgrades from every supported release to the candidate once a previous
    release exists.
- Acceptance criteria:
  - Reapplying migrations is safe, concurrent runners cannot corrupt state, and a
    failed migration has a tested recovery procedure.

### OPS-002 — Safe backup and restore tooling

- Priority: Alpha Blocker for the unsafe command construction; otherwise Alpha Required
- Owner: Operations maintainer + security maintainer
- Problem: The backup drill assumes a python executable and interpolates a
  database URL into python -c/process arguments. Quotes can break the command,
  credentials can leak through process listings, and constructed source creates
  an injection risk.
- Implementation:
  - Build URLs in-process with structured libraries, never by source interpolation.
  - Use the running interpreter or uv run rather than a PATH assumption.
  - Pass secrets through protected environment/file descriptors or native client
    mechanisms and redact subprocess errors.
  - Add password-special-character tests, backup integrity checks, clean restore,
    point-in-time assumptions, retention, and recovery objective documentation.
- Acceptance criteria:
  - Credentials never appear in process argv or logs.
  - Backup and restore succeeds with reserved characters in credentials.
  - A restored stack passes consistency and smoke checks.

### OPS-003 — Health, readiness, graceful shutdown, and observability

- Priority: Alpha Required
- Owner: Operations maintainer + runtime maintainer
- Problem: Container and service readiness, dependency failure, resource bounds,
  and shutdown behavior are not consistently implemented.
- Implementation:
  - Separate liveness from readiness and include migration/dependency readiness.
  - Add bounded timeouts, connection pools, resource limits, queue depth,
    reconciliation lag, webhook delivery, provider cost, and cleanup metrics.
  - Handle SIGTERM with drain deadlines and no new work acceptance.
  - Document alert examples without claiming a bundled production monitoring
    service.
- Acceptance criteria:
  - Orchestrated restart tests show no lost acknowledged work or false-ready
    instances.
  - Metrics and health endpoints do not expose secrets.

### OPS-004 — Operational support and incident procedures

- Priority: Alpha Required
- Owner: Project lead + operations maintainer
- Problem: Public operators need a realistic incident and lifecycle contract.
- Implementation:
  - Publish runbooks for failed migration, runaway provider spend, orphan
    resources, compromised credentials, queue backlog, database loss, webhook
    abuse, and bad release rollback.
  - Define supported versions, EOL, security backports, and severity handling.
  - Provide diagnostic collection with redaction and a public issue/security
    escalation decision tree.
- Acceptance criteria:
  - Tabletop exercises cover spend containment, credential compromise, and bad
    migration recovery.

## 15. Workstream J — documentation and repository hygiene

### DOC-001 — Rewrite public installation and release claims

- Priority: Alpha Blocker
- Owner: Documentation maintainer
- Problem: README/package/image names and production/stability claims do not
  reflect the unavailable package identity, broken images, or unproven public
  artifacts.
- Implementation:
  - Apply the chosen identity consistently.
  - Clearly label the release alpha and enumerate supported/experimental/deferred
    surfaces and providers.
  - Separate contributor-from-source setup from consumer package/image setup.
  - Remove or qualify “used in production” and stability claims until supported
    by releasable artifacts and explicit context.
  - Link to security, privacy/operator data, support, compatibility, and upgrade
    policies.
- Acceptance criteria:
  - Every quickstart path is exercised by TST-005.
  - Claims are bounded, evidence-backed, and consistent across metadata and docs.

### DOC-002 — Correct security, API, and operator documentation

- Priority: Alpha Required
- Owner: Documentation maintainer + domain owners
- Problem: SECURITY.md has placeholders, stale paths and version claims, and
  contradicts fail-closed admin behavior. Other docs contain stale command and
  interface descriptions.
- Implementation:
  - Replace contacts and align supported versions with actual tags.
  - Document secure defaults, auth scopes, network binding, HMAC limitations,
    SSRF controls, rate limits, secrets, and vulnerability reporting.
  - Update REST/MCP/CLI capability tables, webhooks, ports, compose topology,
    migration, backup, retention, and provider limitations.
  - Generate command/API references or enforce them with doc tests.
- Acceptance criteria:
  - Security behavior described in docs is verified by automated tests.
  - No path, command, port, or configuration example is stale.

### DOC-003 — Public terminology and context scrub

- Priority: Alpha Blocker
- Owner: Documentation maintainer + legal/IP reviewer
- Problem: Internal names, planning artifacts, private deployment references, and
  unexplained historical context make provenance and public support boundaries
  ambiguous.
- Implementation:
  - Run a repository-wide terminology audit and classify each hit as public,
    rewritten, removed, or private evidence excluded from artifacts.
  - Remove local agent-memory and evidence files from build contexts and archives.
  - Preserve only history necessary for licensing or transparent attribution.
- Dependencies: GOV-002, PKG-004.
- Acceptance criteria:
  - Legal/IP review approves the public tree and artifact manifests.

### DOC-004 — Documentation quality automation

- Priority: Alpha Required
- Owner: Documentation maintainer + release engineering
- Problem: Internal Markdown links currently pass, but external links, commands,
  configuration schemas, and public hosting assumptions can drift.
- Implementation:
  - Keep internal-link checks and add a tolerant/retriable external-link checker.
  - Test shell snippets and validate referenced files, ports, environment keys,
    API operations, and CLI commands.
  - Decide DG-06 and remove private-runner guidance if unsupported.
  - Add docs ownership and review rules for security/release-sensitive pages.
- Acceptance criteria:
  - Documentation CI produces actionable failures without depending on flaky
    external sites for every pull request; schedule full external checks.

### HYG-001 — Clean-tree and local-tool hygiene

- Priority: Alpha Blocker for releases
- Owner: Release engineering
- Problem: The audit checkout is dirty with local tool configuration and an
  untracked evidence document. Nested ignore behavior did not stop local
  .remember files from entering the sdist.
- Implementation:
  - Preserve intentional code-intelligence configuration but review whether each
    file is public, documented, and free of machine-specific endpoints/secrets.
  - Add top-level ignore/build-exclusion rules for local memory, generated
    evidence, coverage, caches, and local configuration.
  - Make release preflight fail on tracked modifications and unexpected untracked
    content.
  - Do not delete audit evidence until its owner decides its proper private or
    public location.
- Acceptance criteria:
  - Release builds from a clean checkout only and contain no local-machine
    residue.

## 16. Pull-request Definition of Done

Every implementation pull request in this program is complete only when:

- The linked task ID, failure mode, and release priority are stated.
- Behavior, configuration, schemas, docs, examples, and supported-surface matrix
  are updated together.
- Tests include the happy path, authorization boundary, malformed input, relevant
  concurrency/failure path, and regression for the original finding.
- Tests use frozen dependencies and do not silently skip required prerequisites.
- Logs, fixtures, screenshots, and artifacts contain no secrets or private data.
- New dependencies pass vulnerability, license, necessity, and maintenance review.
- Public interfaces include compatibility and upgrade impact.
- Documentation commands are executable and use the selected project identity.
- A named owner reviews security-, migration-, release-, or privacy-sensitive work.
- Verification evidence is attached to the issue or pull request.

## 17. Alpha release rehearsal

Perform the rehearsal from an ephemeral, clean clone with no developer virtual
environment, source-tree imports, private registry credentials, or previously
created compose volumes.

1. Verify the commit, clean tree, branch, signed tag candidate, version, changelog,
   naming, and provenance approvals.
2. Run frozen lint, format, strict typing, SAST, CodeQL, secret detection,
   dependency audit, license policy, unit, integration, contract, mutation, and
   documentation checks.
3. Build wheel and sdist once. Inspect manifests, run metadata checks, compare
   reproducibility hashes, and install each outside the checkout.
4. Initialize and migrate a fresh Postgres database using only the installed
   package resources. Boot the API and run supported CLI/REST/MCP journeys.
5. Build every supported image. Scan it, generate SBOM/provenance, verify non-root
   operation, health, readiness, graceful shutdown, and installed-package
   behavior.
6. Boot the canonical compose stack with unique project/volume names. Run
   migrations, authentication/scope tests, workload journey, lease mutation,
   webhook SSRF/retry/signature tests, reconciliation, retention/purge, backup,
   restore, and upgrade checks.
7. Run live-provider/GPU acceptance only for features the alpha advertises.
   Enforce spend caps and verify teardown/orphan cleanup.
8. Download the exact candidate bytes from the draft GitHub Release and repeat
   consumer smoke journeys without repository access. For any enabled registry
   channel, additionally publish to its test registry and install/deploy by
   immutable version or digest.
9. Generate checksums, per-artifact SBOMs, vulnerability/license reports,
   signatures, provenance, schema diff, test summaries, and repository-settings
   attestation.
10. Hold a go/no-go review with project, security, release, operations, and
    legal/IP owners. Promote the same bytes only after approval.

## 18. Go/no-go checklist

A public alpha is a GO only when every box below is true:

- [ ] DG-01 through DG-06 have approved decision records.
- [ ] Name, repository, package, CLI, and all enabled-channel identifiers are available and consistent.
- [ ] Source and contribution provenance is approved; licensing and notices are complete.
- [ ] No placeholder security/conduct contacts or unexplained internal residue remains.
- [ ] Non-loopback spending and sensitive interfaces fail closed without authentication.
- [ ] Outbound webhook SSRF controls and inbound webhook protections pass adversarial tests.
- [ ] Lease renewal/patch behavior is atomic, persistent, bounded, and consistent across REST/MCP.
- [ ] The worker either performs real queue work with live acceptance or is absent from alpha claims.
- [ ] The frozen dependency audit passes or contains only approved, unexpired VEX records.
- [ ] Wheel and sdist contents pass an explicit policy and migrations work outside the checkout.
- [ ] Every supported image builds, is scanned, runs non-root, and has a verified digest.
- [ ] The canonical stack boots, migrates, becomes ready, completes a user journey, and shuts down cleanly.
- [ ] Required CI is green with no silent readiness skips and deterministic secret scanning.
- [ ] Risk-focused coverage and required mutation thresholds pass.
- [ ] Backup/restore, retention/purge, and sensitive-data redaction are tested.
- [ ] Public docs, templates, ports, commands, badges, and stability claims match reality.
- [ ] Branch protection, vulnerability reporting, secret scanning/push protection, and Dependabot controls are verified.
- [ ] GitHub Release installation succeeds using the exact candidate artifacts;
      test-registry installation/deployment succeeds for every enabled registry channel.
- [ ] Checksums, signatures, SBOMs, provenance, schema diff, and test reports are attached.
- [ ] Release and security backup owners approve the candidate.

Any unchecked Alpha Blocker is an automatic NO-GO. Exceptions require a written
decision that removes the affected feature from the alpha's code path and public
claims; accepting an exposed known defect is not a valid deferral.

## 19. Release evidence bundle

Keep the following immutable evidence for each candidate without committing
secrets or private provider identifiers:

- source commit and signed tag identity;
- clean-tree and artifact-manifest results;
- version/changelog/metadata validation;
- hermetic and live test reports with explicit pass/fail/skip counts;
- coverage and scoped mutation reports;
- SAST, CodeQL, secret, dependency, image, and license reports;
- wheel, sdist, image digests, and reproducibility comparison;
- package/image SBOMs, checksums, signatures, and provenance attestations;
- schema compatibility diff;
- migration, compose, backup/restore, retention/purge, and provider-teardown results;
- legal/IP provenance approval and dependency-license approval;
- repository-security-settings checklist/export;
- named go/no-go approvers and decision.

Evidence containing sensitive operational detail belongs in a protected release
record, not in a public source artifact. Public releases should expose the
non-sensitive verification outputs needed by consumers.

## 20. Risk register

| Risk | Likelihood before remediation | Impact | Mitigation/task | Release treatment |
| --- | --- | --- | --- | --- |
| Publish into or confuse an unrelated PyPI project | High | Critical | DG-01, GOV-001, PKG-001 | Block |
| Unauthenticated provider spend or control | Medium/High | Critical | SEC-001 | Block |
| Webhook SSRF reaches metadata/internal services | High | Critical | SEC-002 | Block |
| Lease endpoint accepts but loses/corrupts mutations | High | High | API-001 | Block |
| Installed wheel cannot migrate | Certain | High | PKG-002, TST-001 | Block |
| Advertised containers fail to build or start | Certain | High | IMG-001, IMG-002 | Block |
| Worker container immediately exits without work | Certain | High | DG-02, WRK-001 | Block or remove feature |
| Vulnerable dependency ships | High | High | SEC-004 | Block unless expiring VEX |
| Local/private files leak through sdist/image context | High | High | PKG-004, HYG-001 | Block |
| Release publishes twice or before tests | Medium | High | REL-001 | Block |
| Readiness report hides skipped live tests | High | High | CI-003 | Block |
| Secrets leak through logs/process argv/database | Medium/High | High | SEC-005, DAT-002, OPS-002 | Block |
| Retention duplicates rather than removes sensitive data | High | High | DAT-003 | Alpha required |
| Unclear copyright or source authority | Unknown | Critical | GOV-002 | Block |
| Single maintainer/security responder becomes unavailable | Medium | High | GOV-004 | Disclose and mitigate |
| GPU/provider tests leave paid resources | Medium | High | TST-004, OPS-004 | Block feature claim |
| Stale SBOM/license report misleads consumers | Certain | Medium/High | SBOM-001, GOV-003 | Alpha required |
| Public docs cause insecure or broken setup | High | High | DOC-001, DOC-002, TST-005 | Block misleading claim |

## 21. Explicit alpha deferrals

The following may be deferred without blocking a narrowly scoped alpha, provided
the limitation is explicit and the unsupported path is not enabled by default:

- broader RunPod CRUD/provider parity beyond the documented alpha journey;
- additional cloud providers and multi-cloud scheduling;
- a richer or actionable TUI;
- Kubernetes/Helm deployment;
- 95% global coverage, provided risk-domain floors in TST-002 pass;
- a CLA, provided the DCO is enforced;
- a project-hosted privacy policy/Terms/DPA when DG-05 confirms there is no hosted
  telemetry, analytics, SaaS, or project-controlled data collection;
- reversible down migrations, provided backup-before-upgrade and tested restore
  are the documented alpha downgrade strategy;
- untested CPU/GPU architectures and combinations;
- the worker itself only under WRK-001 option B, with all release surfaces and
  claims removed.

These are not valid alpha deferrals: package identity conflict, source provenance,
unauthenticated non-loopback spending paths, webhook SSRF, plaintext/reported
secrets, broken migrations, broken advertised images, false readiness success,
known corrupt lease mutations, or uncontrolled release publication.

## 22. Audit-finding traceability matrix

| Audit finding | Remediation IDs |
| --- | --- |
| Occupied PyPI name; unavailable repository URL; branch/remote/tag mismatch | DG-01, GOV-001, PKG-001, CI-005 |
| Wheel omits migrations and uses repository-relative path | PKG-002, TST-001 |
| CLI help/version/no-argument behavior broken | PKG-003, TST-001, TST-005 |
| Five service Dockerfiles fail due to absent README | IMG-001 |
| Floating/root/unlocked images; no .dockerignore/health/provenance | IMG-001, REL-003, SBOM-001 |
| Worker image dependency assumptions | IMG-001, WRK-001 |
| Production compose port mismatch and incomplete topology | IMG-002 |
| MCP/webhook/exporter bind/env defects; blank infrastructure secrets | SEC-001, SEC-003, IMG-002 |
| Hard-coded compose container names collide | IMG-002, CI-005 |
| No API image pipeline; worker build trigger/input/platform mismatch | IMG-003, REL-001 |
| Worker argument mismatch, immediate exit, zero coverage, Redis URL logging | WRK-001, SEC-005, TST-002 |
| REST lease renewal and patch defects; REST/MCP inconsistency | API-001 |
| Outbound webhook SSRF, invalid JSON, retry defect, plaintext secret | SEC-002, API-003, DAT-002 |
| Inbound webhook optional HMAC and abuse limits | SEC-003, SEC-006 |
| Optional data-plane auth and unauthenticated MCP network posture | SEC-001 |
| Vulnerable MCP dependency | SEC-004, DEP-001 |
| Mutable actions and unverified GitHub security settings | SEC-006, CI-004, GOV-005 |
| Formatting failure and warning debt | CI-001, PKG-003, API-002 |
| detect-secrets baseline scope drift and undecided findings | CI-002 |
| Duplicate release triggers and missing readiness dependency | REL-001 |
| Missing version/changelog/artifact/test-registry validation | REL-002, REL-003, REL-004 |
| Non-frozen CI/application dependency installation | DEP-001 |
| Mutation/readiness jobs contradict docs or silently skip | CI-003, TST-002 |
| GitLab private-runner and default-branch assumptions | DG-06, CI-005 |
| Oversized/leaky sdist and dirty-tree release risk | PKG-004, HYG-001 |
| Stale/inconsistent SBOM and license report | SBOM-001, GOV-003 |
| Placeholder community contacts, solo ownership, unenforced DCO | GOV-004, GOV-005 |
| Squashed history and internal/proprietary provenance questions | GOV-002, DOC-003 |
| Issue templates cite nonexistent commands | GOV-005, PKG-003, TST-005 |
| Overstated production/stability claims | DOC-001 |
| Raw payload/error/destination/trace data lacks lifecycle | DAT-001, DAT-004 |
| Archive duplicates retained records and lacks secure lifecycle | DAT-003 |
| Langfuse and hosted-service privacy posture unclear | DG-05, DAT-001 |
| Duplicate OpenAPI IDs and incomplete errors/conformance | API-002 |
| REST/MCP/CLI/provider capability drift and stale stubs | API-004 |
| Global coverage masks worker/webhook/retention/R2 risk | TST-002 |
| No compatibility, clean-machine, built-image, upgrade, or live GPU matrix | TST-001, TST-003, TST-004, TST-005 |
| Backup command interpolation/argv credential exposure | OPS-002, SEC-005 |
| Forward-only migration and undocumented recovery position | OPS-001 |
| Health/readiness/resource/shutdown/observability gaps | OPS-003 |
| Support/EOL/backport/incident posture incomplete | GOV-004, OPS-004, REL-002 |
| Documentation drift and external-link automation gaps | DOC-002, DOC-004 |

## 23. Suggested validation command set

Exact command names may change with DG-01 and implementation, but release
automation should provide one maintained wrapper that performs the equivalent of:

    uv sync --frozen --all-extras --dev
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src
    uv run pytest
    uv run pip-audit
    uv build
    uv run twine check dist/*
    unzip -l dist/*.whl
    tar -tzf dist/*.tar.gz

It should then create a temporary environment outside the checkout, install each
candidate artifact, run CLI/import/migration/API smoke tests, build and scan every
supported image, boot the canonical compose project under a unique name, and run
the release-tier journeys. Release scripts should invoke uv run or the resolved
environment interpreter instead of relying on a bare python executable.

## 24. Program completion criteria

This remediation program is complete for the first public alpha when:

1. Every Alpha Blocker and Alpha Required item is complete or has an approved
   scope-removal decision that eliminates the affected claim and execution path.
2. The go/no-go checklist passes against the exact artifacts to be published.
3. The evidence bundle is complete, internally consistent, and approved.
4. Test-channel consumers can install and operate the project without source-tree
   or private-infrastructure dependencies.
5. The release state machine promotes the same verified bytes exactly once.
6. Public documentation accurately states what the alpha supports, its security
   posture, its data behavior, and its limitations.

After publication, this document should be updated with task links, owners,
completion evidence, and explicit deferrals. New findings should receive a unique
ID, priority, owner, acceptance criteria, and traceability entry rather than being
added as unowned narrative.
