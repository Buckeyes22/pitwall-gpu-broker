# Transitive dependency license review

- Status: Approved by the project owner
- Approval date: 2026-07-18
- Scope: Frozen runtime dependency graph for `pitwall-gpu-broker`
- Exact inventory: `docs/sbom/pitwall-sbom.cdx.json` and release-generated report

The automated policy permits common permissive licenses and rejects unknown,
AGPL, GPL-2.0, GPL-3.0, and SSPL terms unless policy is deliberately changed in
a reviewed pull request. Three runtime packages received explicit approval:

| Package | Detected expression | Approved disposition |
| --- | --- | --- |
| `paramiko` 5.0.0 | `LGPL-2.1` | Unmodified pure-Python source and upstream license are preserved in each published image; Pitwall imposes no restriction on replacement, modification, or reverse engineering of the library |
| `certifi` 2026.5.20 | `MPL-2.0` | Unmodified covered files and upstream license are preserved in each published image; Pitwall files remain separately licensed |
| `tqdm` 4.67.3 | `MPL-2.0 AND MIT` | Unmodified source files and the upstream combined license notice are preserved in each published image; Pitwall files remain separately licensed |

The GitHub Release wheel and sdist do not vendor these dependencies. Exact wheel
inspection found only Pitwall's own LICENSE and NOTICE; installers obtain the
declared dependencies separately. The five GHCR service images do redistribute
the packages. Exact local image inspection confirmed that each installed
distribution preserves its upstream license file, and the packages' Python
source form is present rather than compiled into a combined work. The project
NOTICE names the reviewed components and the SBOM records their exact versions.

On 2026-07-18, the project owner approved these dependencies and this treatment
for the GitHub-first alpha, including GHCR publication. This is the project's
release decision, not an independent legal opinion. Approval is invalidated by
a version/license change, modification or vendoring of a covered component, or
a distribution change that removes source, license, replacement, or notice
availability.

`tools/security/license-policy.json` pins these expressions. A version whose
metadata changes fails CI instead of inheriting an earlier conclusion. Vendored
or modified third-party code is prohibited until its source, changes, license,
notices, and distribution obligations are recorded separately.

This engineering classification and owner approval are not independent legal
advice.
