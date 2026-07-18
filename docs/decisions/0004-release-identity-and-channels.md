# ADR 0004: Public release identity and distribution channels

- Status: Accepted by the project owner; GitHub-first release scope selected
- Date: 2026-07-18
- Decision owner: Project owner

## Decision

The public identity is:

| Surface | Identifier |
| --- | --- |
| Display name | Pitwall |
| Python distribution | `pitwall-gpu-broker` |
| Python import package | `pitwall` |
| CLI | `pitwall-gpu-broker` |
| Repository | `https://github.com/buckeyes22/pitwall-gpu-broker` |
| Container namespace | `ghcr.io/buckeyes22/pitwall-gpu-broker/<service>` |
| Environment prefix | `PITWALL_` (retained) |

The distribution and CLI do not use `pitwall`: that name and executable belong
to an unrelated motorsport package on PyPI. The neutral `pitwall-gpu-broker`
slug also avoids placing a third-party cloud-provider trademark in the product,
package, repository, or image identity. Keeping the import package avoids a
large internal namespace migration and is not a distribution or shell command.

The first public alpha channels are GitHub source, a GitHub prerelease containing
the verified wheel, sdist, checksums, SBOM, and provenance evidence, and GHCR for
the five supported service images. The project owner does not currently have
PyPI or TestPyPI accounts, so both Python registries are explicitly deferred and
do not block the GitHub-first alpha. GHCR is approved and required for the first
alpha; the GitHub prerelease is created only after the exact verified images are
published successfully. No project-hosted SaaS, package mirror, or GPU worker
image is offered.

## Search and owner-approval record

On 2026-07-18, the project owner confirmed that the project will be published
personally and selected the neutral identity above. The PyPI and TestPyPI JSON
APIs returned 404 for `pitwall-gpu-broker`, and the GitHub API returned 404 for
`github.com/buckeyes22/pitwall-gpu-broker`. The bare `pitwall` PyPI distribution
is occupied, and public web searches show unrelated products using “Pitwall,”
so the project does not claim exclusive rights in the display word. The
disambiguating distribution/repository slug and GPU-broker description are
mandatory public identifiers.

These are point-in-time technical searches, not registry reservations. The
project owner accepts the residual naming risk for the first public alpha; no
third-party trademark is intentionally used as part of the product identity.

Before setting `PITWALL_RELEASE_ENABLED=true`, configure the protected
`ghcr-staging`, `ghcr`, and `github-release` environments. PyPI and TestPyPI are
not implemented by the current release workflow; adding them requires a later
decision and trusted-publishing setup.

## Compatibility and rollback

Artifacts are immutable. A bad Python release is yanked and superseded; a bad
image tag is deprecated and consumers move to a new digest. Existing bytes are
never replaced. Pre-1.0 breaking changes require changelog and schema-diff
approval. Security removals may happen without deprecation when necessary to
make an unsafe path fail closed.
