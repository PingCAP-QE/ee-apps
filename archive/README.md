# Archived Projects

This directory holds retired subprojects from the EE applications monorepo.
They are kept for reference and history only:

- **Not built:** the path filters in `.github/workflows/ci.yaml` and
  `release.yaml` list active subprojects only; nothing under `archive/` is
  selected, so no image is built.
- **Not packaged or published:** no Helm chart or release entry points here.
- **Not deployed:** `PingCAP-QE/ee-ops` does not reference these directories.
- **Not scanned:** CodeQL ignores `archive/**` through
  [`.github/codeql/codeql-config.yml`](../.github/codeql/codeql-config.yml).

Git history remains the authoritative record, and each project keeps its
original layout under `archive/<project>/`.

| Project | Archived | Reason |
|---------|----------|--------|
| [`change-insight/`](change-insight/) | 2026-10-31 | Dormant configuration-change analysis service; no chart, CI build, or deployment. |

To revive an archived project, move it back to the repository root, restore its
CI path filters and chart, and update this table.

<!-- codeql archive-exclusion verification probe; safe to discard -->
