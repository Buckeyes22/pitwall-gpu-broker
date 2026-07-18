# Repository settings

The GitHub repository should remain practical for a solo maintainer:

| Control | State |
| --- | --- |
| `main` | Pull requests, required CI and CodeQL checks, linear history, no force-push or deletion |
| Reviews | Optional while the project has one maintainer |
| Release tags | Protected signed `v*` tags; no deletion or force-update |
| Actions | Read-only default token and immutable action references |
| Releases | Protected tag-only environments; publication disabled by default |
| Security | Private reporting, secret scanning, push protection, and Dependabot enabled |
| Packages | GHCR uses the workflow `GITHUB_TOKEN`; no personal registry token |

Recheck these settings after changing repository administrators or release
automation.
