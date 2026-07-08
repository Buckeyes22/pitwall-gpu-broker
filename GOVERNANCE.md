# Governance

Pitwall is currently a single-maintainer project. The maintainer
sets roadmap and release scope, reviews contributions, triages security reports,
and has final say when consensus cannot be reached. This is an explicit
bus-factor risk, not a promise of staffed support.

## Decisions

Routine changes use pull-request discussion and rough consensus. Changes to
security boundaries, compatibility, data handling, release identity, supported
deployment surfaces, or governance require an architecture decision record in
`docs/decisions/`. The maintainer records alternatives and resolves a deadlock
in writing. Contributors affected by a conflict must disclose it and recuse
themselves from the final approval.

## Maintainer changes and succession

New maintainers need a sustained record of technically sound, respectful
contributions and explicit approval from the current maintainer. A maintainer
may resign at any time. Six months without repository activity is considered
inactive unless announced otherwise. Access is removed promptly after
resignation, compromise, or sustained violation of project policy.

The public alpha must not be released until a second eligible person is assigned
as the backup for both release and confidential-report handling, or the release
owner explicitly accepts and records the residual single-maintainer risk in the
go/no-go record. Registry, repository, and trusted-publisher access must be
recoverable without relying on one workstation.

## Contributions and licensing

Contributions use the Developer Certificate of Origin. Every commit must carry
a valid `Signed-off-by` trailer and pass the required DCO check. A contributor
license agreement is not required. Adding one later would require a public
governance decision explaining the need and treatment of earlier contributions.

Security embargoes may temporarily limit disclosure. Otherwise, project
decisions and release evidence are public. See `MAINTAINERS.md`, `SECURITY.md`,
`SUPPORT.md`, and `CONTRIBUTING.md`.
