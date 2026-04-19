# CI Dashboard CronJobs

This directory documents recurring jobs for the CI dashboard.

## Daily Flaky Issue Sync

`sync-flaky-issues` keeps `ci_l1_flaky_issues` in sync from the read-only `github_tickets` source and enriches branch information by calling the GitHub API when the source ticket payload does not carry branch details.

Render a CronJob manifest:

```bash
cd ci-dashboard
./scripts/render_flaky_issue_sync_cronjob.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-backfill-db \
  --github-secret prow-github \
  --ca-secret ci-dashboard-backfill-ca \
  > /tmp/ci-dashboard-sync-flaky-issues.yaml
```

Apply it:

```bash
kubectl create -f /tmp/ci-dashboard-sync-flaky-issues.yaml
```

Useful overrides:

- `--schedule "0 2 * * *"` keeps the default once-per-day run.
- `--time-zone Asia/Shanghai` keeps scheduling aligned with the local working timezone.
- `--github-secret prow-github` injects `GITHUB_TOKEN` for GitHub API lookups during branch enrichment.
- `--batch-size 200` is a safe default because the flaky issue set is small.
- `--concurrency-policy Forbid` avoids overlapping sync runs.

Validation:

```bash
kubectl -n apps get cronjob ci-dashboard-sync-flaky-issues
kubectl -n apps get jobs -l app.kubernetes.io/name=ci-dashboard-sync-flaky-issues
kubectl -n apps logs job/<latest-job-name>
```

Notes:

- The job is idempotent: rerunning the same issue set updates existing rows in `ci_l1_flaky_issues`.
- Existing source tables remain read-only; only `ci_l1_flaky_issues` and `ci_job_state` are written.
