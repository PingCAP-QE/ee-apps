# Tencent Jenkins error archive repair

## Cause and fix

The GCP archive CronJob sets
`CI_DASHBOARD_JENKINS_INTERNAL_BASE_URL=http://jenkins.jenkins.svc.cluster.local`.
Previously every controller URL was rewritten to that host. Tencent
`https://do.pingcap.net/jenkins/...` builds were fetched from the GCP controller,
causing missing-build errors or, if the build number existed on both controllers,
potentially archiving the wrong log.

`JenkinsClient.build_api_url` now applies this override only to the exact
`prow.tidb.net` hostname. Tencent retains its original HTTPS URL. The rule covers
progressive console text, failed-stage/node logs, console signal excerpts, and
Jenkins timings. Keep the GCP internal-base setting; no new endpoint setting or
schema migration is required.

Error Catalog separately excludes effective
`OTHERS / SUPERSEDED_BY_NEWER_BUILD` normal cancellations. This corrects its error
denominator; it does not recover missing failure classifications.

## Deploy and verify before backfill

1. Publish the tested app and jobs images. Derive the release tag from the
   successful ee-apps release workflow output and verify both images in GHCR;
   do not guess a tag from the date or use `latest`.
2. Update the CI Dashboard release in ee-ops to that verified tag. Confirm Flux
   reconciliation and the live app, archive, and analyzer images.
3. With production Job execution authorized, run two small archive/analyze smoke
   Jobs using the released jobs image, existing `ci-dashboard` service account,
   DB secret, GCS bucket, and analyzer LLM configuration. Example known row IDs:

   ```bash
   # Run inside the smoke Job, not on a local computer.
   python -m ci_dashboard.jobs.cli archive-error-logs --build-id 50010227
   python -m ci_dashboard.jobs.cli analyze-errors --build-id 50010227
   python -m ci_dashboard.jobs.cli archive-error-logs --build-id 50010231
   python -m ci_dashboard.jobs.cli analyze-errors --build-id 50010231
   ```

   These are Tencent `ghpr_build/1201` and `ghpr_unit_test/1193`. Before running,
   recheck their row IDs, URLs, and current archive/revision fields. Each command
   returns summary counts; stop on nonzero `builds_failed` or an expected row
   being skipped, even if the CLI process exits zero.
4. Check archive logs: requests for these builds must use `do.pingcap.net`, never
   the GCP controller. Confirm the uploaded artifact was redacted and belongs to
   the correct Tencent build (timestamp/PR/SHA), and that the machine category is
   now populated. Never print credentials or publish raw console logs.
5. Check Error Catalog shares, trends, rankings, drilldowns, and coverage use the
   same cancellation-excluding population. Missing logs must remain visible in
   coverage, not be interpreted as an INFRA rate of zero.

## Historical repair scope

Start with `TENCENT`, jobs `pingcap/tidb/ghpr_build` and
`pingcap/tidb/ghpr_unit_test`, inclusive dates `2026-09-01..2026-10-06`. Use this
read-only preflight, then expand to other Tencent jobs only after the pilot:

```sql
SELECT
  job_name, build_system, state, COUNT(*) AS build_count,
  SUM(log_gcs_uri IS NOT NULL) AS archived_count,
  SUM(error_l1_category IS NOT NULL OR revise_error_l1_category IS NOT NULL)
    AS categorized_count
FROM ci_l1_builds
WHERE cloud_phase = 'TENCENT'
  AND job_name IN ('pingcap/tidb/ghpr_build', 'pingcap/tidb/ghpr_unit_test')
  AND start_time >= '2026-09-01' AND start_time < '2026-10-07'
  AND state IN ('failure', 'error', 'timeout', 'timed_out', 'aborted')
  AND NOT (
    COALESCE(revise_error_l1_category, error_l1_category, 'OTHERS') = 'OTHERS'
    AND COALESCE(revise_error_l2_subcategory, error_l2_subcategory, 'UNCLASSIFIED')
      = 'SUPERSEDED_BY_NEWER_BUILD'
  )
GROUP BY job_name, build_system, state;
```

Select missing-archive row IDs for each bounded window:

```sql
SELECT id, normalized_build_url, log_gcs_uri
FROM ci_l1_builds
WHERE cloud_phase = 'TENCENT'
  AND build_system = 'JENKINS'
  AND job_name IN ('pingcap/tidb/ghpr_build', 'pingcap/tidb/ghpr_unit_test')
  AND start_time >= :window_start AND start_time < :window_end_exclusive
  AND state IN ('failure', 'error', 'timeout', 'timed_out', 'aborted')
  AND NOT (
    COALESCE(revise_error_l1_category, error_l1_category, 'OTHERS') = 'OTHERS'
    AND COALESCE(revise_error_l2_subcategory, error_l2_subcategory, 'UNCLASSIFIED')
      = 'SUPERSEDED_BY_NEWER_BUILD'
  )
  AND log_gcs_uri IS NULL
  AND revise_error_l1_category IS NULL AND revise_error_l2_subcategory IS NULL
ORDER BY start_time, id;
```

- Execute repair inside a durable GKE coordinator Job, with one sequential child
  window of at most five inclusive days at a time, pinned to the released image
  digest. Do not use a local shell loop to submit or perform the backfill.
- Within each window, invoke existing `archive-error-logs --build-id ID`, verify
  its summary/DB result, then `analyze-errors --build-id ID`. Keep Jenkins fetches
  serial, persist progress, and stop on the first failure or uncertain result.
- Preserve human revisions and source tables. Do not use `sync-builds` as an
  error-log repair; it neither archives Jenkins logs nor classifies them.
- Do not blindly change `build_system='UNKNOWN'`: report these rows separately
  for source linkage validation; the archive job intentionally accepts only
  `JENKINS` rows.
- Audit existing Tencent GCS artifacts too: old host rewriting could fetch a
  same-number GCP build. Re-archive/re-analyze with `--force` only for explicitly
  verified incorrect artifacts, with no human revision. Never bulk-force all
  categories; list fetch failures and uncertain artifacts for review.
- Final checks: missing archive count decreases, classified failures increase,
  reviewed values are unchanged, logs match Tencent builds, and catalog totals
  match the same effective-category SQL population. Any remaining no-log or
  unclassified failures must be reported, not silently omitted from the result.

This runbook prepares the rollout and recovery; it does not submit production
Jobs or imply that historical data has already been repaired.
