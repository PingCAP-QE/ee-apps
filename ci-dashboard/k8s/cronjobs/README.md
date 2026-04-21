# CI Dashboard CronJobs

This directory documents recurring jobs for the CI dashboard.

## Hourly Pod Sync

`sync-pods` incrementally reads Cloud Logging `k8s_pod` events and writes:

- `ci_l1_pod_events`
- `ci_l1_pod_lifecycle`
- `ci_job_state`

Render a CronJob manifest:

```bash
cd ci-dashboard
./scripts/render_sync_pods_cronjob.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-db \
  --ca-secret ci-dashboard-ca \
  --gcp-project pingcap-testing-account \
  --service-account ci-dashboard \
  --suspend true \
  > /tmp/ci-dashboard-sync-pods.yaml
```

Apply it:

```bash
kubectl apply -f /tmp/ci-dashboard-sync-pods.yaml
```

Recommended first bring-up:

```bash
kubectl -n apps create job --from=cronjob/ci-dashboard-sync-pods ci-dashboard-sync-pods-smoke-$(date +%s)
kubectl -n apps logs job/<smoke-job-name> --follow
```

Useful overrides:

- `--schedule "15 * * * *"` keeps the default hourly run.
- `--pod-event-namespaces "prow-test-pods"` pins the actual pod event namespace in the current prow cluster.
- `--lookback-minutes 240` widens the first smoke run window when needed.
- `--overlap-minutes 15` keeps a safe reread buffer for logging lag.
- `--max-pages 200` caps one run's Logging API scan.
- `--concurrency-policy Forbid` avoids overlapping sync runs.
- `--suspend true` is recommended for the first apply until smoke validation passes.

Validation:

```bash
kubectl -n apps get cronjob ci-dashboard-sync-pods
kubectl -n apps get jobs -l app.kubernetes.io/name=ci-dashboard-sync-pods
kubectl -n apps logs job/<latest-job-name>
```

Notes:

- The job is idempotent: rerunning the same overlap window updates existing event/lifecycle rows instead of creating duplicate source-event rows.
- Existing upstream/source tables remain read-only.
- Production auth should rely on GKE runtime identity for Cloud Logging access rather than a static access token.

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
