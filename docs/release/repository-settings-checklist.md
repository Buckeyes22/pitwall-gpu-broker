# Repository settings audit

Run this checklist before every release and after any administrator change.
Record screenshots or API output privately; do not store tokens in the repo.

| Control | Required state |
| --- | --- |
| Default branch | `main`; deletion and force-push disabled |
| Release tags | Active `v*` ruleset; verified commits required; deletion and force-updates disabled |
| Pull requests | At least one approval, stale approvals dismissed, CODEOWNERS required |
| Required checks | lint, format, typecheck, tests, integration, coverage, security, mutation, CodeQL, DCO, release readiness |
| Actions | GitHub-hosted runners; read-only default token; approved immutable actions only |
| Releases | Protected environments; tag-triggered workflow; `PITWALL_RELEASE_ENABLED` off by default |
| Security | Private reporting, secret scanning, push protection, Dependabot alerts/updates enabled |
| Community | Discussions enabled; question/support links route there |
| Packages | Immutable versions/tags; trusted publisher or `GITHUB_TOKEN` only |
| Recovery | Two-factor authentication and recovery ownership verified |

The workflow policy checker validates versioned YAML controls. Hosting controls
must be checked through GitHub settings or API because they are not represented
fully in git.
