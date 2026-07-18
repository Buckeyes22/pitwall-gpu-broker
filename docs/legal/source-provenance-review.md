# Source provenance review record

- Status: Approved by the project owner and sole author
- Inventory date: 2026-07-17
- Owner attestation date: 2026-07-18
- Release effect: Provenance gate satisfied for the GitHub-first alpha

## Inventory

| Category | Repository locations | Required publication basis |
| --- | --- | --- |
| Python source and SQL migrations | `src/`, `db/migrations/` | Author/contributor right to license under Apache-2.0 |
| Tests and generated fixtures | `tests/` | Original or generated non-confidential material |
| Deployment and automation | `docker/`, Compose, `.github/`, `scripts/`, `tools/` | Original configuration; third-party Actions pinned and separately licensed |
| Documentation and examples | `README.md`, `docs/`, `examples/` | Original documentation; quoted/copied content attributed and compatible |
| Seed/sample data | `seed/`, test fixtures | Synthetic values only; no customer or production payloads |
| Dependency metadata | `pyproject.toml`, `uv.lock`, SBOM | Third-party packages under their own licenses |

No binary media, model weights, datasets, fonts, or vendored third-party source
are intended to ship in the Python artifacts. Build inspection enforces an
allowlist. Local audit notes are not part of package or release artifacts and
must not be committed.

## Owner attestation and approval

On 2026-07-18, the project owner attested that all publishable code,
documentation, migrations, configuration, examples, tests, and other material
were authored personally or generated under the owner's direction; no employer,
client, contractor, or outside contributor has an ownership claim; and no
proprietary or private source was copied into the release. Third-party
dependencies and pinned Actions remain under their own licenses and are not
claimed as project-authored work.

As the sole author and rights holder, the project owner approved licensing the
inventoried project material under Apache-2.0 and approved the current NOTICE
for the GitHub-first alpha. This self-attestation satisfies the project's
provenance gate; it is not represented as independent legal advice. The gate
must be reopened if contrary evidence appears or if a future release includes
another contributor, employer-controlled work, copied material, or a new asset
category.

This record does not claim an independent legal opinion or broader trademark
clearance. It records the sole author's ownership and publication decision for
the inventoried release tree.
