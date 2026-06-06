# Traceability

This file maps stable business rules to implementation files and tests in the current worktree.

| Rule | Implementation | Current Test Coverage |
| --- | --- | --- |
| `BR-01` Build field derivation | [sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_builds.py) | [test_sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_sync_builds.py) |
| `BR-02` Build URL normalization | [build_url_matcher.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/build_url_matcher.py) | [test_build_url_matcher.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_build_url_matcher.py) |
| `BR-03` Cloud phase classification | [build_url_matcher.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/build_url_matcher.py) | [test_build_url_matcher.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_build_url_matcher.py) |
| `BR-04` Exact retest parsing | [retest_parser.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/retest_parser.py) | [test_retest_parser.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_retest_parser.py) |
| `BR-05` PR event import rules | [sync_pr_events.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_pr_events.py) | placeholder coverage in [test_job_placeholders.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_job_placeholders.py) |
| `BR-06` Failure category classification | [sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_builds.py) and planned [refresh_build_derived.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/refresh_build_derived.py) | [test_sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_sync_builds.py) plus placeholder coverage in [test_job_placeholders.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_job_placeholders.py) |
| `BR-07` Flaky case evidence matching | planned [refresh_build_derived.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/refresh_build_derived.py) | not implemented yet |
| `BR-08` Flaky flag computation | planned [refresh_build_derived.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/refresh_build_derived.py) | not implemented yet |
| `BR-09` Watermark processing | [state_store.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/state_store.py), [sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_builds.py) | [test_state_store.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_state_store.py), [test_sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_sync_builds.py) |
| `BR-10` Local testability | [config.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/common/config.py), [db.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/common/db.py), [tests/conftest.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/conftest.py) | [test_config_and_db.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/test_config_and_db.py), [test_sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_sync_builds.py) |

Current local validation command:

```bash
cd /Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard
PYTHONPATH=src ./.venv/bin/python -m pytest --cov=src/ci_dashboard --cov-report=term-missing
```
