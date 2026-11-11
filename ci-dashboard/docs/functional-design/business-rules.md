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
| `url` starts with `https://prow.tidb.net/` | `GCP` |
| all other URLs | `TENCENT` |

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
- retry build-linked PRs without a `pr_snapshot` independently of the build/ticket watermarks, including Jenkins-only builds without a `source_prow_row_id`
- use the existing idempotent upsert when a previously missing ticket becomes available; no new build or newer ticket timestamp is required
- refresh an existing snapshot whenever that PR's source `updated_at` is newer than its snapshot `event_time` after parsing both as UTC-naive timestamps and truncating to whole seconds, even if the update arrives behind another PR's global ticket timestamp; no new build is required
- truncate snapshot timestamps to whole seconds before writing to the second-precision `event_time` column; unchanged fractional or offset timestamps must not trigger repeated rewrites
- retain `last_ticket_updated_at` as the maximum observed ticket timestamp for reporting, not as a completeness filter across independently imported repositories
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

Build-level rate queries:

- Keep the stored retry-recovery flags unchanged, but exclude a build from flaky numerators when its effective L1 error category is `INFRA`: `revise_error_l1_category` takes precedence over `error_l1_category`.
- Missing error categories retain the existing flag-based behavior. INFRA builds remain in the failure-like denominator; neither this denominator nor blind-retry-loop counting changes.
- Apply the same eligible flaky predicate to period summaries, trends, job rankings, and the flaky component of combined noisy/affected-PR metrics. Read the category at query time so classification updates require no flag recomputation or backfill.
- This does not introduce a new rule for counting failures without a subsequent same-commit pass, or change testcase evidence and stored V1 `failure_category` classification.

## BR-09: Watermark-Based Incremental Processing

All jobs must:

- read watermark at job start
- process incremental data above the watermark or touched since the watermark, plus explicit completeness retries such as build-linked PRs missing a snapshot
- upsert by logical unique key
- update watermark only after successful write transaction
- keep the completed new-build high-water mark separate from the cursor through a pending historical PR/case refresh; historical work must not move the new-build high-water mark backward or make unrelated old builds appear new
- freeze a pending refresh's source targets until its selected work is complete; later source changes belong to a subsequent refresh
- never mutate upstream source tables

## BR-10: Local Testability

The data-layer implementation must support local unit tests without TiDB Cloud by allowing:

- sqlite-backed local tests
- deterministic fixtures for `prow_jobs`, `ci_l1_builds`, and `ci_job_state`
- local execution via `PYTHONPATH=src ./.venv/bin/python -m pytest`

## BR-11: Flaky Case Weekly Metrics

- Case evidence queries include PR and postsubmit CI builds, without requiring a PR event row.
- Scope testcase evidence by `problem_case_runs.repo` and `problem_case_runs.branch`, regardless of the repository or branch that triggered the CI build. A PD or cloud-storage-engine job running TiDB/master tests belongs to TiDB/master testcase metrics.
- Preserve normalized URL, cloud phase, and the 24-hour report/start-time attribution guards. Explicit job/cloud filters still apply to the actual CI build. Build-level rates and `has_flaky_case_match` retain their existing trigger-repository semantics.
- Discover case-to-job membership from attributable historical problem records through the query end, independently of the selected start date. Scope each denominator estimate by the evidenced trigger repository, job, and trigger branch; same-named jobs or unevidenced trigger branches must not inflate it. Follow those scopes across clouds unless an explicit cloud filter is supplied.
- Resolve trigger branches from the uniquely keyed PR snapshot (`event_key = 'pr_snapshot'` and `event_type = 'pr_snapshot'`) when available, then stored build `target_branch`, then `base_ref`; stale timeline-event branches are not authoritative. Missing trigger branches remain a separate unknown scope; do not substitute the testcase branch. Trigger branch metadata never overrides a reported testcase branch.
- Count weekly flaky evidence and estimated job builds only inside the selected build-time window.
- `total_runs_est` is a job-build estimate, not a complete testcase execution ledger: it cannot distinguish fresh executions, cached results, or attempts.
- When `total_runs_est = 0`, return `flaky_rate_pct = null` and `N/A (0/0)`, not an observed zero percent. Unknown weeks must not qualify as evidence of two flaky-free weeks.
- Preserve the issue table's existing `ignores_branch=true` contract; match each row's metrics to its own issue branch.

## BR-12: Top Flaky Cases by Reported Build Count

- The Top 10 table is independent of the Issue-tracked weekly table and loads via
  `/api/v1/flaky/top-cases`. It follows the common date-window and filter validation.
- Select `problem_case_runs.flaky = 1` using the tested `repo` and `branch`, and
  `report_time` in the selected inclusive UTC date range. Unlike the weekly
  evidence charts, this ranking does not move reports into their build-start window.
- Rank `(repo, case_name)` by distinct reported build identity, deduplicating repeated
  reports and suites in the same build. Normalize URL aliases and legacy Tencent
  keys; retain cloud/controller identity so equal paths in different clouds stay
  separate. Ignore records without a case name or a usable build key.
- Unselected repo/branch means all repos/branches; never combine same-named cases
  from different repos. Return at most ten rows with stable repo/case tie-breaking.
- Job selection requires an attributable running build, matching normalized URL,
  cloud and the existing 24-hour report/start-time guard. Its trigger repo/branch
  need not equal the tested repo/branch. Without a job filter, source reports do not
  require an L1 build row. Cloud selection follows the report's build controller.
- “Affected builds” means builds where this flaky case was reported, including builds
  that ultimately recover to success. It is neither a final-build-state count nor a rate.
  Each row returns at most the 10 most recent distinct reported builds, newest report
  first, on the controller that reported the case; truncation never changes the count.
- Match tracked Issues by repo and case name only. Include open and closed Issues
  regardless of their creation branch or lifetime; do not discard evidence because
  a master Issue is closed. Retain multiple associations and unmatched cases.
- Associations are testcase-level matches, not confirmation of the same root cause;
  “No linked issue” does not establish that GitHub has no relevant Issue.
- Issue-status and bucket-granularity filters do not change this table. Existing
  Issue-tracked and build-scoped panels retain their separate contracts.
