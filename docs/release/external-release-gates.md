# Public release checklist

Pitwall is a solo-maintained, self-hosted project. Publishing does not depend on
another maintainer, an outside reviewer, a PyPI account, or the project owner's
RunPod credentials.

Before creating a release tag:

- [ ] `main` is clean and its required GitHub checks pass.
- [ ] The version, changelog, and versioned release notes agree.
- [ ] The secret scan and dependency/license checks pass.
- [ ] The local artifact and container smoke tests pass.
- [ ] `ghcr-staging`, `ghcr`, and `github-release` allow only protected `v*` tags.
- [ ] `PITWALL_RELEASE_ENABLED=true` is set only for the release window.

The tag-triggered workflow then builds and verifies the wheel, source archive,
SBOMs, checksums, attestations, and five GHCR images before creating the GitHub
prerelease. It uses GitHub's short-lived workflow token for GHCR; it never reads
or requests a RunPod API key.

PyPI and TestPyPI are optional future channels and are not present in the
current release workflow.

After publication, install the GitHub artifacts in a clean environment and pull
each image by digest. If a release is bad, publish a new version instead of
replacing an existing tag, artifact, or digest.
