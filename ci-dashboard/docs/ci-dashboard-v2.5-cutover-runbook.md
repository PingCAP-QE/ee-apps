# CI Dashboard V2.5 Cutover Runbook

Status: Draft v0.1

Last updated: 2026-04-24

Scope:
- V2.5 canonical build URL cutover
- `ci_l1_builds.normalized_build_key` -> `normalized_build_url`
- `ci_l1_pod_lifecycle.normalized_build_url` backfill + old pod linkage column cleanup
- `sync-pods` pod-name fallback + in-job reconcile
- maintenance-window rollout with short downtime / data lag

Related files:
- `sql/013_cutover_canonical_build_urls.sql`
- `src/ci_dashboard/jobs/cli.py`
- `src/ci_dashboard/jobs/sync_pods.py`
- `scripts/render_sync_pods_cronjob.sh`
- `scripts/render_backfill_job.sh`
- `charts/ci-dashboard`

## 1. Goal

Cut over the running dashboard and jobs to the V2.5 schema and code in one maintenance window, without long-lived dual-write.

After cutover:
- build-side and pod-side both persist `normalized_build_url`
- Jenkins URLs are stored in canonical public form such as:
  - `https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1718/`
- `/display/redirect` variants are removed
- `sync-pods` can backfill linkage from:
  - live K8s metadata
  - `pod_name` parsing
  - delayed build arrival via reconcile

## 2. Rollout Model

This runbook assumes the agreed V2.5 rollout model:
- merge code first
- wait for new app/jobs images to be ready
- enter a short maintenance window
- pause recurring jobs
- apply schema migration
- run one-off backfill / reconcile jobs
- deploy the new app/jobs workloads
- smoke test
- resume recurring jobs

Important boundary:
- after the schema migration finishes, old V1/V2 images must not be resumed against the migrated schema
- rollback after migration is therefore a forward-fix on V2.5, not a schema rollback to old images

## 3. Inputs

Fill these before the window:

```bash
export APP_TAG="<app-image-tag>"
export JOBS_TAG="<jobs-image-tag>"
export K8S_NAMESPACE="apps"
export APP_RELEASE_NAME="ci-dashboard"
export DB_SECRET_NAME="ci-dashboard-eq-prd-insight-db"
export CA_SECRET_NAME="ci-dashboard-backfill-ca"
export SERVICE_ACCOUNT_NAME="ci-dashboard"
export GCP_PROJECT="pingcap-testing-account"
export BACKFILL_JOB_NAME="ci-dashboard-v25-backfill-$(date -u +%Y%m%d%H%M%S)"
export POD_RECONCILE_JOB_NAME="ci-dashboard-v25-pod-reconcile-$(date -u +%Y%m%d%H%M%S)"

# Choose the historical range you want the one-off jobs to cover.
# These are examples only.
export BACKFILL_START_DATE="<YYYY-MM-DD>"
export BACKFILL_END_DATE="<YYYY-MM-DD>"
export POD_RECONCILE_START_DATE="<YYYY-MM-DD>"
export POD_RECONCILE_END_DATE="<YYYY-MM-DD>"
```

If you need a quick helper for the date ranges:

```sql
SELECT DATE(MIN(start_time)) AS first_build_date, DATE(MAX(start_time)) AS last_build_date
FROM ci_l1_builds;

SELECT DATE(MIN(scheduled_at)) AS first_pod_date, DATE(MAX(scheduled_at)) AS last_pod_date
FROM ci_l1_pod_lifecycle;
```

## 4. Preconditions

Before starting the window, all of the following should be true:

- V2.5 code has been merged.
- new app image and jobs image have been built and pushed
- local validation for this revision is green
  - `PYTHONPATH=src pytest -q`
- the target DB is reachable with a `mysql` client
- the DB secret exposes usable TiDB credentials
  - ideally `TIDB_HOST`, `TIDB_PORT`, `TIDB_USER`, `TIDB_PASSWORD`, `TIDB_DB`
  - if the secret only exposes `CI_DASHBOARD_DB_URL`, prepare an equivalent mysql client connection path before the window
- the app release path is ready
  - either your normal `ee-ops` release flow
  - or a direct Helm command against `charts/ci-dashboard`
- the jobs release path is ready
  - existing recurring CronJobs that use the jobs image must all be updated to `JOBS_TAG` before resuming

Current recurring jobs that should be considered in the pause/resume set:
- `ci-sync-builds`
- `ci-sync-pr-events`
- `ci-refresh-build-derived`
- `ci-sync-pods`
- `ci-sync-flaky-issues`

## 5. Preflight

From the repo root:

```bash
cd /Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2.5/ci-dashboard
PYTHONPATH=src pytest -q
```

Render the new `sync-pods` CronJob manifest ahead of time in suspended mode:

```bash
./scripts/render_sync_pods_cronjob.sh \
  --image "ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:${JOBS_TAG}" \
  --db-secret "${DB_SECRET_NAME}" \
  --ca-secret "${CA_SECRET_NAME}" \
  --gcp-project "${GCP_PROJECT}" \
  --service-account "${SERVICE_ACCOUNT_NAME}" \
  --pod-event-namespaces "prow-test-pods,jenkins-tidb,jenkins-tiflow" \
  --suspend true \
  > /tmp/ci-dashboard-sync-pods-v25.yaml
```

Render the one-off build backfill Job manifest:

```bash
./scripts/render_backfill_job.sh \
  --job-command backfill-range \
  --start-date "${BACKFILL_START_DATE}" \
  --end-date "${BACKFILL_END_DATE}" \
  --image "ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:${JOBS_TAG}" \
  --db-secret "${DB_SECRET_NAME}" \
  --ca-secret "${CA_SECRET_NAME}" \
  --job-name "${BACKFILL_JOB_NAME}" \
  > /tmp/ci-dashboard-v25-backfill.yaml
```

Render the one-off pod reconcile Job manifest:

```bash
./scripts/render_backfill_job.sh \
  --job-command reconcile-pod-linkage-range \
  --start-date "${POD_RECONCILE_START_DATE}" \
  --end-date "${POD_RECONCILE_END_DATE}" \
  --image "ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:${JOBS_TAG}" \
  --db-secret "${DB_SECRET_NAME}" \
  --ca-secret "${CA_SECRET_NAME}" \
  --job-name "${POD_RECONCILE_JOB_NAME}" \
  > /tmp/ci-dashboard-v25-pod-reconcile.yaml
```

Optional dry-run of the app chart from this repo:

```bash
helm template "${APP_RELEASE_NAME}" ./charts/ci-dashboard \
  --namespace "${K8S_NAMESPACE}" \
  --set image.tag="${APP_TAG}" \
  --set secretEnvFrom[0]="${DB_SECRET_NAME}" \
  --set ssl.caSecretName="${CA_SECRET_NAME}" \
  > /tmp/ci-dashboard-app-v25.yaml
```

If production app release is managed in `ee-ops`, prepare the equivalent image tag update there instead of applying the chart directly from this repo.

## 6. Start The Maintenance Window

Capture the current CronJob specs before changing anything:

```bash
mkdir -p /tmp/ci-dashboard-v25-preflight
for cj in \
  ci-sync-builds \
  ci-sync-pr-events \
  ci-refresh-build-derived \
  ci-sync-pods \
  ci-sync-flaky-issues
do
  kubectl -n "${K8S_NAMESPACE}" get cronjob "${cj}" -o yaml > "/tmp/ci-dashboard-v25-preflight/${cj}.yaml" || true
done
```

Pause recurring jobs:

```bash
for cj in \
  ci-sync-builds \
  ci-sync-pr-events \
  ci-refresh-build-derived \
  ci-sync-pods \
  ci-sync-flaky-issues
do
  kubectl -n "${K8S_NAMESPACE}" patch cronjob "${cj}" \
    --type merge \
    -p '{"spec":{"suspend":true}}' || true
done
```

Confirm there are no actively running CI dashboard Jobs you still need to wait for:

```bash
kubectl -n "${K8S_NAMESPACE}" get cronjob
kubectl -n "${K8S_NAMESPACE}" get jobs | rg "ci-dashboard|ci-sync|ci-refresh"
```

## 7. Apply The Schema Migration

Prepare local DB env files from the K8s secrets:

```bash
./scripts/prepare_local_tidb_env.sh \
  --namespace "${K8S_NAMESPACE}" \
  --db-secret "${DB_SECRET_NAME}" \
  --ca-secret "${CA_SECRET_NAME}"
```

Load the generated env:

```bash
set -a
source ./.local/tidb.env
set +a
```

If `./.local/tidb.env` contains only `CI_DASHBOARD_DB_URL` and not the individual `TIDB_*` fields, stop here and prepare an equivalent mysql client connection before continuing.

Apply the cutover SQL:

```bash
MYSQL_PWD="${TIDB_PASSWORD}" mysql \
  --connect-timeout=10 \
  --host="${TIDB_HOST}" \
  --port="${TIDB_PORT}" \
  --user="${TIDB_USER}" \
  --ssl-ca="${TIDB_SSL_CA}" \
  "${TIDB_DB}" \
  < sql/013_cutover_canonical_build_urls.sql
```

Immediate schema checks:

```sql
SHOW COLUMNS FROM ci_l1_builds LIKE 'normalized_build_url';
SHOW COLUMNS FROM ci_l1_pod_lifecycle LIKE 'normalized_build_url';
SHOW COLUMNS FROM ci_l1_builds LIKE 'normalized_build_key';
SHOW COLUMNS FROM ci_l1_pod_lifecycle LIKE 'jenkins_build_url_key';
SHOW COLUMNS FROM ci_l1_pod_lifecycle LIKE 'normalized_build_key';
```

Expected:
- the two `normalized_build_url` columns exist
- old pod linkage columns are gone
- old build-side `normalized_build_key` is gone

## 8. Canonical URL Verification

Run these checks immediately after migration:

```sql
SELECT COUNT(*) AS build_rows,
       SUM(normalized_build_url IS NOT NULL) AS build_rows_with_url
FROM ci_l1_builds;

SELECT COUNT(*) AS pod_rows,
       SUM(normalized_build_url IS NOT NULL) AS pod_rows_with_url
FROM ci_l1_pod_lifecycle;

SELECT COUNT(*) AS redirect_rows
FROM ci_l1_builds
WHERE normalized_build_url LIKE '%/display/redirect%';

SELECT COUNT(*) AS non_canonical_build_rows
FROM ci_l1_builds
WHERE normalized_build_url IS NOT NULL
  AND normalized_build_url NOT LIKE 'https://prow.tidb.net/jenkins/job/%'
  AND normalized_build_url NOT LIKE 'https://prow.tidb.net/view/gs/%';
```

Expected:
- `redirect_rows = 0`
- `non_canonical_build_rows = 0`

Sample recent rows:

```sql
SELECT source_prow_job_id, url, normalized_build_url
FROM ci_l1_builds
WHERE normalized_build_url IS NOT NULL
ORDER BY start_time DESC
LIMIT 20;

SELECT pod_name, source_prow_job_id, normalized_build_url
FROM ci_l1_pod_lifecycle
ORDER BY scheduled_at DESC
LIMIT 20;
```

## 9. Run One-Off V2.5 Data Jobs

Run the build backfill Job:

```bash
kubectl apply -f /tmp/ci-dashboard-v25-backfill.yaml
kubectl -n "${K8S_NAMESPACE}" get jobs -l app.kubernetes.io/name=ci-dashboard-backfill
kubectl -n "${K8S_NAMESPACE}" logs job/"${BACKFILL_JOB_NAME}" --follow
```

Run the pod reconcile Job:

```bash
kubectl apply -f /tmp/ci-dashboard-v25-pod-reconcile.yaml
kubectl -n "${K8S_NAMESPACE}" logs job/"${POD_RECONCILE_JOB_NAME}" --follow
```

Notes:
- `backfill-range` is stateless and does not update incremental `ci_job_state`
- `reconcile-pod-linkage-range` is also stateless and is safe to rerun for the same date window
- both jobs are idempotent for the selected ranges

## 10. Deploy New App And Jobs

### 10.1 App

If you are doing a direct Helm release from this repo:

```bash
helm upgrade --install "${APP_RELEASE_NAME}" ./charts/ci-dashboard \
  --namespace "${K8S_NAMESPACE}" \
  --set image.tag="${APP_TAG}" \
  --set secretEnvFrom[0]="${DB_SECRET_NAME}" \
  --set ssl.caSecretName="${CA_SECRET_NAME}"
```

If production app rollout is managed elsewhere, update that release to `APP_TAG` now and wait until the new app Pod is ready.

Readiness checks:

```bash
kubectl -n "${K8S_NAMESPACE}" rollout status deploy/"${APP_RELEASE_NAME}" --timeout=10m
kubectl -n "${K8S_NAMESPACE}" get pods -l app.kubernetes.io/name=ci-dashboard
```

### 10.2 Jobs

Apply the new `sync-pods` CronJob manifest:

```bash
kubectl apply -f /tmp/ci-dashboard-sync-pods-v25.yaml
kubectl -n "${K8S_NAMESPACE}" get cronjob ci-dashboard-sync-pods -o wide
```

Update the other recurring jobs that use the shared jobs image to `JOBS_TAG` through their normal release path before resuming them:
- `ci-sync-builds`
- `ci-sync-pr-events`
- `ci-refresh-build-derived`
- `ci-sync-flaky-issues`

## 11. Smoke Test Before Resuming Schedule

Keep `ci-sync-pods` suspended for the first smoke:

```bash
kubectl -n "${K8S_NAMESPACE}" create job \
  --from=cronjob/ci-dashboard-sync-pods \
  "ci-dashboard-sync-pods-v25-smoke-$(date +%s)"
```

Watch logs:

```bash
kubectl -n "${K8S_NAMESPACE}" logs job/<smoke-job-name> --follow
```

Check:
- the job exits successfully
- summary logs show non-zero work when recent pod events exist
- no SQL errors about missing old columns

Verify job state:

```sql
SELECT job_name, last_status, last_started_at, last_succeeded_at, last_error
FROM ci_job_state
WHERE job_name IN (
  'ci-sync-builds',
  'ci-sync-pr-events',
  'ci-refresh-build-derived',
  'ci-sync-pods',
  'ci-sync-flaky-issues'
)
ORDER BY job_name;
```

## 12. Resume Recurring Jobs

Once the app, build-side jobs, and `sync-pods` smoke all look good, resume the paused CronJobs:

```bash
for cj in \
  ci-sync-builds \
  ci-sync-pr-events \
  ci-refresh-build-derived \
  ci-sync-pods \
  ci-sync-flaky-issues
do
  kubectl -n "${K8S_NAMESPACE}" patch cronjob "${cj}" \
    --type merge \
    -p '{"spec":{"suspend":false}}' || true
done
```

Confirm:

```bash
kubectl -n "${K8S_NAMESPACE}" get cronjob
```

## 13. Post-Cutover Validation

DB checks:

```sql
SELECT COUNT(*) AS unresolved_recent_pods
FROM ci_l1_pod_lifecycle
WHERE scheduled_at >= DATE_SUB(NOW(), INTERVAL 72 HOUR)
  AND (source_prow_job_id IS NULL OR source_prow_job_id = '');

SELECT COUNT(*) AS recent_canonical_jenkins_rows
FROM ci_l1_pod_lifecycle
WHERE scheduled_at >= DATE_SUB(NOW(), INTERVAL 72 HOUR)
  AND normalized_build_url LIKE 'https://prow.tidb.net/jenkins/job/%';

SELECT COUNT(*) AS recent_redirect_rows
FROM ci_l1_pod_lifecycle
WHERE scheduled_at >= DATE_SUB(NOW(), INTERVAL 72 HOUR)
  AND normalized_build_url LIKE '%/display/redirect%';
```

API checks:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/status/freshness
```

Dashboard/query checks:
- build trend pages still load
- flaky pages still load
- migration comparison still returns canonical job URLs
- no route or query still expects `normalized_build_key` or `jenkins_build_url_key`

## 14. Failure Handling

If something fails before the schema migration:
- keep CronJobs suspended
- revert the image/manifests as needed
- restart the window later

If something fails after the schema migration:
- do not resume old V1/V2 jobs or app images
- keep the CronJobs suspended
- fix forward on the V2.5 code/image path
- rerun:
  - `backfill-range`
  - `reconcile-pod-linkage-range`
  - `sync-pods` smoke

Safe reruns:
- the migration SQL is written for one-time cutover and should not be blindly re-applied without checking current schema state
- the one-off backfill and reconcile jobs are safe to rerun for the same date ranges
- `sync-pods` recurring logic remains overlap-safe and idempotent

## 15. Completion Criteria

The cutover is complete when all of the following are true:

- app is serving successfully on V2.5 code
- recurring jobs are all running on `JOBS_TAG`
- `ci_l1_builds.normalized_build_url` and `ci_l1_pod_lifecycle.normalized_build_url` are populated and canonical
- old pod linkage columns are absent
- `sync-pods` smoke succeeds
- post-cutover validation queries are clean
- paused CronJobs have been resumed
