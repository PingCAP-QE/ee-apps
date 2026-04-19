# One-Off Backfill Job

This directory contains the minimum assets needed to initialize dashboard data on the
current GKE prow cluster with a one-off Kubernetes Job.

The local `kubectl` context currently points to:

- `gke_pingcap-testing-account_us-central1-c_prow`

The render script defaults to namespace `apps`, which matches the current Prow control-plane
deployments on that cluster. Override `--namespace` if your target namespace is different.

## Files

- `Dockerfile.jobs`: container image for data jobs and one-off backfills
- `db-secret.example.yaml`: example secret carrying DB connection env vars
- `ca-secret.example.yaml`: optional CA bundle secret when TiDB requires custom SSL trust
- `scripts/render_backfill_job.sh`: render a one-off Job manifest for `backfill-range`

## 1. Build And Push The Jobs Image

From the project root:

```bash
cd /Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard
docker build -f Dockerfile.jobs -t ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> .
docker push ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag>
```

## 2. Create The DB Secret

Start from the example file and apply it:

```bash
kubectl apply -f /Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/k8s/backfill/db-secret.example.yaml
```

If TiDB needs a custom CA, apply the CA secret too:

```bash
kubectl apply -f /Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/k8s/backfill/ca-secret.example.yaml
```

## 3. Render And Create The One-Off Job

This example backfills everything starting on `2025-12-01`:

```bash
cd /Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard
./scripts/render_backfill_job.sh \
  --start-date 2025-12-01 \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-backfill-db \
  --ca-secret ci-dashboard-backfill-ca \
  > /tmp/ci-dashboard-backfill.yaml

kubectl create -f /tmp/ci-dashboard-backfill.yaml
```

If you do not need a custom CA, just omit `--ca-secret`.

If you want to backfill a closed date range instead of open-ended import:

```bash
./scripts/render_backfill_job.sh \
  --start-date 2025-12-01 \
  --end-date 2025-12-31 \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-backfill-db \
  > /tmp/ci-dashboard-backfill.yaml
```

## 4. Watch Progress

```bash
kubectl -n apps get jobs -l app.kubernetes.io/name=ci-dashboard-backfill
kubectl -n apps logs job/<job-name> -f
```

The one-off Job runs the stateless command:

```bash
python -m ci_dashboard.jobs.cli backfill-range --start-date ...
```

This is idempotent for the selected time window:

- `ci_l1_builds` is upserted by `source_prow_job_id`
- `ci_l1_pr_events` is upserted by `(repo, pr_number, event_key)`
- derived flags are recomputed for the selected build window
- incremental `ci_job_state` watermarks are not updated by `backfill-range`

## 5. Quick Verification

After the Job succeeds:

```sql
SELECT COUNT(*) AS build_count, MIN(start_time), MAX(start_time)
FROM ci_l1_builds
WHERE start_time >= '2025-12-01 00:00:00';
```

```sql
SELECT COUNT(*) AS pr_event_count
FROM ci_l1_pr_events;
```

```sql
SELECT source_prow_row_id, repo_full_name, pr_number, job_name, state,
       target_branch, is_flaky, is_retry_loop, failure_category
FROM ci_l1_builds
WHERE start_time >= '2025-12-01 00:00:00'
ORDER BY source_prow_row_id DESC
LIMIT 20;
```
