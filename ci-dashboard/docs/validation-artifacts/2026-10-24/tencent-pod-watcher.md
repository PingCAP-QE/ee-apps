# Shared GCP/Tencent Pod watcher validation

Date: 2026-10-24. Baseline: `b383a9c9a6c2f2bffcb1064834689f200f851d37`.

Design: [Tencent Pod watcher](../../tencent-pod-watcher-design.md).
Cost assumptions: [cost estimate](../../tencent-pod-watcher-cost-estimate.md).

## Independent review gates

Both gates used the installed `pi-deepseek` wrapper with read-only review tools;
only Codex authored or fixed files. Neither loop committed, pushed, deployed,
created a PR, or merged anything.

| Gate | Result |
| --- | --- |
| Design | One review/fix/adjudication round; final `ADJUDICATION: STOP`, no open or deferred findings |
| Code | One review round; final `VERDICT: CLEAN`, Critical/Warnings/Suggestions all empty; no fixer/adjudicator needed |

The design loop resolved compatibility wording, exact first/latest Event time
semantics, reconciliation transaction ownership, Tencent-only reason scoping,
and the dependency-free renderer test contract. The implementation was then
reviewed against that contract, not just the earlier design prose.

An earlier background code-review worker was quota-blocked before executing any
review. It was **not** counted as a pass. The successful code loop ran directly
on 2026-10-24 and completed in 6m28s. Old design fixer/adjudicator output was not
used as code-review evidence.

Local detailed evidence is archived in the ignored `.review-loop/design-pass/`
and `.review-loop/code-pass/` directories, including reviewed-file hashes. The
reviewed design SHA-256 is
`bc6c4ffcb9d1cc6e10f25cadac41ff7e572e49f19a3475681b0d296a42bb5d6f`.

## Final offline verification

From `ci-dashboard`, with Python 3.14 and the repository's existing dependencies:

```bash
COVERAGE_FILE=/tmp/tencent-watcher-reviewed.coverage PYTHONPATH=src python -m pytest \
  -o addopts='' -q --cov=src/ci_dashboard --cov-report=term
python -m ruff check src tests
bash -n scripts/render_pod_watcher_deployment.sh scripts/render_pod_watcher_rbac.sh
python -m compileall -q src
```

- Full Python suite: **926 passed**.
- Whole-project statement coverage: **91.15%** (9,213/10,108), above the configured
  90% threshold. Baseline coverage was 90.90%.
- Changed Python modules combined: **92.42%** (2,232/2,415); this is neither
  whole-project nor changed-line coverage.
- Full Ruff, Bash syntax, Python compilation and `git diff --check`: passed.
- Repository pre-commit hooks: end-of-file fixer, trailing-whitespace and gitleaks
  hardcoded-secret detection passed on the changed files.
- Coverage output was kept outside tracked files; `ci-dashboard/.coverage` was
  not changed.

The full suite includes 127 offline renderer subprocess cases and dual-cloud
SQLite linkage, replay, timestamp, snapshot, authentication and bounded
reconciliation regressions. No YAML dependency was added. Python 3.14 emitted
existing SQLite adapter-deprecation/fixture-resource warnings; no test failed.

## Boundaries

- MySQL/TiDB validation covers generated SQL and assignment ordering, not execution
  against a live server.
- No CLS activation, cloud changes, deployment, schema migration, historical
  key rewrite, or API/frontend changes were performed.
- Live DB schema, network/TLS/grants, registry/image availability, GitOps ownership,
  GCP before/after attribution audit and bounded reconciliation scheduling remain
  rollout gates in the design.
- Collision preflight rejects visible bad keys. The unchanged URL-only Runtime
  Insights join cannot guarantee safety against contradictory facts inserted
  after a previously valid/pending URL was saved; trusted-key audit/controlled
  repair remains required. This is not a claim of lossless Event capture or a
  global future-data invariant.
