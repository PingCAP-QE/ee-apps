# Functional Design

This directory captures the data-layer functional design in a form that stays close to code and tests.

Document split:

- `domain-entities.md`: owned tables and their data contracts
- `business-rules.md`: stable business rules referenced by code and tests
- `business-logic-model.md`: job-by-job execution logic
- `traceability.md`: mapping from rules to implementation files and unit tests

Scope:

- V1 data layer only
- aligned to the current worktree implementation under `ci-dashboard/src/ci_dashboard`
- intended to complement, not replace, the higher-level documents in:
  - `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/ci-dashboard-v1-design.md`
  - `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/ci-dashboard-v1-implementation.md`
