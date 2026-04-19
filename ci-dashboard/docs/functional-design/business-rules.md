# Business Rules

## BR-01: Build Field Derivation

| Field | Rule |
| --- | --- |
| `repo_full_name` | `org + '/' + repo` |
| `is_pr_build` | `1` when `pr_number IS NOT NULL` |
| `head_sha` | from `spec.refs.pulls[0].sha` when present |
| `pending_time` | from `status.pendingTime` or `status.pending_time` |
| `build_id` | from `status.build_id` or `status.buildId` |
| `pod_name` | from `status.pod_name` or `status.podName` |
| `queue_wait_seconds` | `pending_time - start_time`, else `NULL` |
| `run_seconds` | `completion_time - pending_time`, else `NULL` |
| `total_seconds` | `completion_time - start_time`, else `NULL` |

## BR-02: Build URL Normalization

Apply in order:

1. remove prefix `https://do.pingcap.net`
2. remove prefix `https://prow.tidb.net`
3. remove suffix `/display/redirect`
4. trim trailing `/`

Example:

- input: `https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/display/redirect`
- output: `/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299`

## BR-03: Cloud Phase Classification

| Condition | `cloud_phase` |
| --- | --- |
| `url` starts with `https://prow.tidb.net/jenkins/` | `GCP` |
| all other URLs | `IDC` |

## BR-04: Exact Retest Command Parsing

Supported commands:

- `/retest`
- `/retest-required`

Algorithm:

1. trim leading and trailing whitespace
2. collapse repeated whitespace
3. compare exact normalized body

Accepted regex:

- `^/(retest|retest-required)$`

Explicit exclusion:

- comments such as `say /retest to rerun ...` are not retest events

## BR-05: PR Event Import Rules

- import only build-linked PRs
- persist one synthetic `pr_snapshot` row per tracked PR
- import timeline events only for:
  - `committed`
  - exact retest command comments
- derive:
  - `target_branch` from `branches.base.ref`
  - `head_ref` from `branches.head.ref`
  - `head_sha` from `branches.head.sha`
- allow missing `github_tickets` rows without failing the job
- when the source PR ticket row is missing, keep build-side PR metadata incomplete rather than fabricating fallback values
- V1 does not require PR-event completeness for `pingcap/docs`, `pingcap/docs-cn`, or `PingCAP-QE/ci`
- short source lag on `github_tickets` is acceptable in V1

## BR-06: Failure Category Classification in V1

| Condition | `failure_category` |
| --- | --- |
| `is_flaky = 1 OR is_retry_loop = 1` | `FLAKY_TEST` |
| all other cases | `NULL` |

Presentation note:

- `NULL` is displayed as `UNCLASSIFIED`

V1 explicitly does not assign:

- `INFRA`
- `CODE_DEFECT`

## BR-07: Flaky Case Evidence Matching

Set `has_flaky_case_match = 1` when all conditions hold:

- `problem_case_runs.flaky = 1`
- `problem_case_runs.repo = ci_l1_builds.repo_full_name`
- normalized `problem_case_runs.build_url = ci_l1_builds.normalized_build_key`
- `problem_case_runs.report_time` is within:
  - `ci_l1_builds.start_time`
  - `ci_l1_builds.start_time + 24 hours`

## BR-08: Flaky Flag Computation

Group key:

- `(repo_full_name, pr_number, job_name, head_sha)`

Pilot logic carried into V1:

- consecutive failures before a pass on the same group may be `is_flaky = 1`
- repeated retests without resolution may be `is_retry_loop = 1`

Status:

- rule is confirmed at design level
- implementation is deferred to `ci-refresh-build-derived`

## BR-09: Watermark-Based Incremental Processing

All jobs must:

- read watermark at job start
- process only incremental data above the watermark or touched since the watermark
- upsert by logical unique key
- update watermark only after successful write transaction
- never mutate upstream source tables

## BR-10: Local Testability

The data-layer implementation must support local unit tests without TiDB Cloud by allowing:

- sqlite-backed local tests
- deterministic fixtures for `prow_jobs`, `ci_l1_builds`, and `ci_job_state`
- local execution via `PYTHONPATH=src ./.venv/bin/python -m pytest`
