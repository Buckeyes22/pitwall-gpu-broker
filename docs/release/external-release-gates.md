# External public-alpha release gates

Repository automation cannot prove the controls below. Every box requires a
dated owner and evidence link in the private go/no-go record. Do not set the
repository variable `PITWALL_RELEASE_ENABLED=true` until all applicable boxes
are complete.

- [x] Project owner selected the neutral `Pitwall` / `pitwall-gpu-broker`
      identity and accepted the dated naming search record in ADR 0004.
- [x] The project owner selected a GitHub-first alpha with GHCR image
      publication. PyPI and TestPyPI are deferred and do not gate this release.
- [x] The project owner and sole author approved source/contribution provenance,
      Apache-2.0 publication authority, and NOTICE in the dated provenance record.
- [x] The project owner approved the exact dependency-license report, including
      unmodified Paramiko, certifi, and tqdm distribution in GHCR images.
- [x] Canonical `https://github.com/buckeyes22/pitwall-gpu-broker` repository exists
      publicly with `main` default (verified by the project owner on 2026-07-18).
- [ ] Branch rules require CI, CodeQL, DCO, review, and no force-push/deletion.
- [ ] Private Vulnerability Reporting, secret scanning, push protection,
      Dependabot alerts, and security updates are enabled and tested.
- [ ] GitHub Discussions is enabled for public support questions and its contact link works.
- [ ] Protected `ghcr-staging`, `ghcr`, `github-release`, and
      `live-provider-acceptance` environments have least-privilege reviewers.
      Python registry environments are required only when that deferred channel
      is enabled.
- [ ] GitHub Release permissions, attestations, and GHCR package permissions are
      bound to the exact repository/workflow/environment. Python registry trusted
      publishers are required only when that deferred channel is enabled.
- [ ] A backup release/security maintainer has tested recovery access, or the
      project lead records explicit acceptance of the single-maintainer risk.
- [ ] Live RunPod acceptance passes with an approved spending cap and cleanup.
- [ ] The exact GitHub Release wheel and sdist pass isolated install/smoke tests,
      and the exact GHCR staging image digests pass pull/smoke tests. TestPyPI is
      required only when that deferred channel is enabled.
- [ ] GitHub Release withdrawal/supersession, image-deprecation, and credential-
      revocation procedures are tested. PyPI yank is deferred with that channel.
- [ ] Signed tag, release notes, compatibility result, SBOM, checksums,
      attestations, and clean-clone evidence are approved at go/no-go.

Any unchecked item is a publication blocker. A later release may attach the
completed evidence without changing this reusable checklist.
