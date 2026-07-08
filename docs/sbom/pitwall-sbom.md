# Software bill of materials

`pitwall-sbom.cdx.json` is a source-tree snapshot of the frozen runtime graph
for `pitwall-gpu-broker`. Generate it with the exact release lock:

```bash
uv export --frozen --preview-features sbom-export --format cyclonedx1.5 \
  --no-dev --no-emit-project --output-file docs/sbom/pitwall-sbom.cdx.json
```

The committed snapshot is review evidence, not the sole release inventory. The
release workflow also generates an SPDX SBOM from the installed wheel and one
for each of the five built container images, then attaches checksums and GitHub
attestations. Those artifact-derived files are authoritative for a release.

`tools/security/check_licenses.py` walks the installed runtime dependency graph,
rejects unknown/denied licenses, and fails if a review-required package changes
license. Its report states explicitly that final legal approval is an external
release gate.

SBOM timestamps and serial numbers identify a generation event and are not
expected to be byte-reproducible. Package/image artifact reproducibility is
verified separately by rebuilding and comparing bytes.
