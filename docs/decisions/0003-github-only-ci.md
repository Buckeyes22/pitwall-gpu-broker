# ADR 0003: GitHub Actions is the only public CI platform

- Status: Accepted for the public alpha
- Date: 2026-07-17
- Decision owner: Project maintainer

## Context

The GitLab configuration required an internal privileged runner tagged
`docker`, assumed runner-level service isolation, and drifted independently from
GitHub Actions. A public contributor or fork could not reproduce that pipeline.

## Decision

GitHub Actions on GitHub-hosted runners is the sole supported public CI and
release platform. The private-runner GitLab file is removed. GitLab users may
build their own pipeline from the versioned local commands, but the project does
not claim parity or provide publishing credentials for it.

The canonical default branch is `main`. Workflows use frozen dependencies,
immutable action commits, explicit permissions and concurrency, and unique
Compose project names. Release identity is available only to protected,
tag-triggered publication jobs.

## Consequences

There is one authoritative CI matrix and no private infrastructure prerequisite.
Adding another supported platform requires a new decision, public runners, and
parity tests. Registry support for operator-supplied GitLab container images is
unrelated and remains available.
