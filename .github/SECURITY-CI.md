# Security CI Checklist

Reference: `SECURITY.md`

The following settings must be enabled manually in the GitHub repository UI (under **Settings → Security**):

- [ ] **Secret scanning** — Scans commits, PRs, and issues for committed secrets
- [ ] **Push protection** — Blocks commits containing detected secrets from entering the repository
- [ ] **Dependabot alerts** — Notifies when vulnerabilities are detected in dependencies
- [ ] **Dependabot security updates** — Automatically creates PRs to patch vulnerabilities
- [ ] **Private vulnerability reporting** — Allows external researchers to report security issues via a form

## Action Pinning Recommendation

Third-party GitHub Actions should be pinned to full commit SHAs rather than version tags to prevent supply-chain attacks:

```yaml
steps:
  - uses: actions/checkout@a5ac7e51b41094c92402da3b24376905380afc29  # v4
```

Always verify the first 7 characters of the SHA match the official published action tag.
