# Releasing Pitwall

The project is pre-1.0 alpha software. A tag is not permission to publish:
production jobs also require protected environments and the repository variable
`PITWALL_RELEASE_ENABLED=true`. Keep that variable unset until every item in
`docs/release/external-release-gates.md` is approved.

## Version and compatibility

`pyproject.toml` is the version source of truth. Alpha tags use
`v0.MINOR.PATCHaN`; the changelog must contain the same version and a date no
later than the run date. Follow `docs/compatibility.md`. Never change an applied
migration or overwrite an existing package version or image digest.

## Prepare the candidate

1. Start from a clean clone of `main` and run `uv sync --frozen --extra dev`.
2. Update the version, lockfile, changelog, support/capability matrix, and any
   compatibility notes in one reviewed pull request.
3. Run the local quality, security, integration, mutation, build, Compose, and
   artifact-smoke gates. Complete the live provider lane with an approved spend
   cap and verify cleanup.
4. Complete the external gate and repository-settings checklists. Confirm
   trusted publishers and protected environments identify this exact repository
   and workflow.
5. Create a signed annotated tag only after the go/no-go approval:

   ```bash
   git tag -s v0.1.0a1 -m "Pitwall 0.1.0a1"
   git push origin v0.1.0a1
   ```

## Automated state machine

The tag-triggered release workflow performs the GitHub-first state machine:

1. Full hermetic/integration/release readiness and required live acceptance.
2. Tag, version, date, and clean-source validation.
3. Reproducible wheel/sdist build, metadata/content inspection, and clean install
   of both artifact forms with packaged migrations.
4. Build, scan, inventory, and save the five supported service images: API,
   reconciler, webhook receiver, cost exporter, and MCP.
5. Checksums, SBOMs, GitHub artifact attestations, and immutable candidate upload.
6. Staging GHCR push/pull smoke, followed by publication and attestation of the
   same verified image bytes.
7. A prerelease GitHub Release containing the wheel, sdist, checksums, SBOM, and
   evidence, created only after GHCR publication succeeds.

PyPI and TestPyPI are deferred channels. They require
`PITWALL_PYPI_RELEASE_ENABLED=true` in addition to the main release gate. Leave
that variable unset until their accounts, projects, trusted publishers,
environments, and rehearsal requirements are complete. GHCR is part of the
first-alpha state machine and is controlled by the main release gate.

There is no project GPU worker image in the alpha. See ADR 0002.

## Verification and rollback

After publication, download the wheel and sdist from the GitHub Release, install
each into a fresh environment, verify `pitwall-gpu-broker --version`, run the
artifact/migration smoke, and verify checksums and GitHub attestations. Pull every
GHCR image by digest, confirm non-root operation, and verify documented health
behavior. Record the results in the release evidence bundle.

If a GitHub release artifact is defective, mark the prerelease withdrawn and
publish a corrected version; never reuse the version or tag. If a later PyPI
package is defective, yank it and publish a new version. If an image is
defective, mark its tag unsupported and move consumers to a new digest; never
replace an immutable digest. Stop GHCR publication, or a later optional Python
registry channel, when its rehearsal or any earlier gate fails. Follow
`docs/operator/upgrade-recovery.md` for database-aware rollback.

For a security hotfix, branch from the last acceptable tag, make the smallest
reviewed change, update the changelog/version, rerun the complete state machine,
and publish an advisory. Security urgency does not authorize mutable artifacts
or bypass provenance evidence.
