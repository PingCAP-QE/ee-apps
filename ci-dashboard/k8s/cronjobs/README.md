# CI Dashboard CronJobs

This directory documents recurring jobs for the CI dashboard.

## Jenkins Event Worker

`consume-jenkins-events` is the long-running V3 worker that consumes Kafka topic
`jenkins-event`, writes event audit rows into `ci_l1_jenkins_build_events`, and
creates or enriches canonical rows in `ci_l1_builds`.

Render a Deployment manifest:

```bash
cd ci-dashboard
./scripts/render_jenkins_worker_deployment.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-eq-prd-insight-db \
  --kafka-bootstrap-servers cluster-cd-kafka-bootstrap:9092 \
  --service-account ci-dashboard \
  > /tmp/ci-dashboard-jenkins-worker.yaml
```

Apply it:

```bash
kubectl apply -f /tmp/ci-dashboard-jenkins-worker.yaml
```

Recommended first bring-up:

```bash
kubectl -n apps rollout status deployment/ci-dashboard-jenkins-worker
kubectl -n apps logs deployment/ci-dashboard-jenkins-worker --follow
```

Useful overrides:

- `--kafka-topic jenkins-event` keeps the dedicated Jenkins topic.
- `--kafka-group-id ci-dashboard-v3-jenkins-worker` keeps consumer identity explicit.
- `--finished-event-type dev.cdevents.pipelinerun.finished.0.1.0` pins the accepted finished event family.
- `--replicas 1` is the safest first production rollout.

Validation:

```bash
kubectl -n apps get deployment ci-dashboard-jenkins-worker
kubectl -n apps get pods -l app.kubernetes.io/name=ci-dashboard-jenkins-worker
kubectl -n apps logs deployment/ci-dashboard-jenkins-worker --tail=200
```

Notes:

- The Deployment should only be rolled out after the jobs image includes the V3 `consume-jenkins-events` command.
- The worker is safe to restart because dedup happens by `event_id` in `ci_l1_jenkins_build_events`.
- After a finished event is committed to TiDB, the worker submits one bounded,
  asynchronous `/timings/` fetch. Fetch or parse failures are logged and do not
  fail the Kafka event.
- Historical repair is an explicit adhoc operation, for example
  `ci-dashboard backfill-jenkins-timings --lookback-days 30`; there is no
  recurring timings CronJob.
- Current cluster values we already verified:
  - Kafka bootstrap service: `cluster-cd-kafka-bootstrap:9092`
  - Jenkins service DNS for internal callers: `http://jenkins.jenkins.svc.cluster.local`

## Pod Watcher

`watch-pods` is the long-running pod lifecycle collector. It runs in `apps` by
default and records Pod `metadata.creationTimestamp` as `pod_created_at`.

Preconditions:

- apply `017_alter_ci_l1_pod_lifecycle_add_pod_created_at.sql`
- grant `apps/ci-dashboard` `get/list/watch` on `pods` and `events` in watched namespaces

```bash
cd ci-dashboard
./scripts/render_pod_watcher_rbac.sh \
  --service-account-namespace apps \
  --service-account-name ci-dashboard \
  --target-namespaces prow-test-pods,jenkins-tidb,jenkins-tiflow \
  | kubectl apply -f -
```

Render and apply the Deployment:

```bash
./scripts/render_pod_watcher_deployment.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-db \
  --ca-secret ci-dashboard-ca \
  --gcp-project pingcap-testing-account \
  --service-account ci-dashboard \
  --cluster-name prow \
  --location us-central1-c \
  --db-batch-size 100 \
  > /tmp/ci-dashboard-pod-watcher.yaml
kubectl apply -f /tmp/ci-dashboard-pod-watcher.yaml
kubectl -n apps rollout status deployment/ci-dashboard-pod-watcher
```

DB persistence tuning:

- `CI_DASHBOARD_POD_WATCH_DB_BATCH_SIZE` controls rows per DB write batch. Default: `100`; use a positive integer.
- `CI_DASHBOARD_POD_WATCH_DB_RETRY_ATTEMPTS` controls retry attempts for retryable DB write errors. Default: `3`; use a positive integer.
- `CI_DASHBOARD_POD_WATCH_DB_RETRY_BASE_DELAY_MS` controls the first retry delay in milliseconds before exponential backoff. Default: `500`; use a positive integer.
- `CI_DASHBOARD_POD_WATCH_DB_RETRY_MAX_DELAY_MS` caps the exponential backoff delay in milliseconds. Default: `5000`; use a positive integer.

Validation:

```bash
kubectl -n apps get deployment ci-dashboard-pod-watcher
kubectl -n apps get pods -l app.kubernetes.io/name=ci-dashboard-pod-watcher
kubectl -n apps logs deployment/ci-dashboard-pod-watcher --tail=200
```

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
  --db-secret ci-dashboard-eq-prd-insight-db \
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

## Jenkins Error Log Archive

`archive-error-logs` scans terminal non-success Jenkins builds, fetches a
bounded log tail through Jenkins progressive text, redacts it in memory, and
uploads one effective artifact to GCS.

Render a CronJob manifest:

```bash
cd ci-dashboard
./scripts/render_archive_error_logs_cronjob.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-eq-prd-insight-db \
  --service-account ci-dashboard \
  --gcs-bucket <gcs-bucket> \
  --jenkins-internal-base-url http://jenkins.jenkins.svc.cluster.local \
  --suspend true \
  > /tmp/ci-dashboard-archive-error-logs.yaml
```

Apply it:

```bash
kubectl apply -f /tmp/ci-dashboard-archive-error-logs.yaml
```

Recommended first bring-up:

```bash
kubectl -n apps create job --from=cronjob/ci-dashboard-archive-error-logs ci-dashboard-archive-error-logs-smoke-$(date +%s)
kubectl -n apps logs job/<smoke-job-name> --follow
```

Useful overrides:

- `--schedule "35 * * * *"` keeps the default hourly archive cadence.
- `--build-limit 100` caps one run's Jenkins and GCS load.
- `--log-tail-bytes 262144` keeps the default tail size at 256 KiB.
- default object layout is `YYMM/<build_id>.log`, for example `2604/1682039.log`.
- `--gcs-prefix ci-dashboard/jenkins` is optional if we later want an extra stable prefix in front of the month folder.
- `--jenkins-secret <secret>` enables server-side Jenkins auth if console access is later restricted.
- `--suspend true` is recommended until GCS write permission is confirmed.

Validation:

```bash
kubectl -n apps get cronjob ci-dashboard-archive-error-logs
kubectl -n apps create job --from=cronjob/ci-dashboard-archive-error-logs ci-dashboard-archive-error-logs-smoke-$(date +%s)
kubectl -n apps logs job/<smoke-job-name>
```

Notes:

- The CronJob should only be rolled out after the jobs image includes the V3 `archive-error-logs` command.
- The current `ci-dashboard` GKE service account does not yet appear to have confirmed GCS write access, so bucket IAM must be granted before unsuspending the recurring CronJob.
- The first slice is intentionally serial: no fetch concurrency flag is required for bring-up.

## Daily Unattached Block Volume Sync

`sync-unattached-block-volumes` snapshots unattached block volumes for the Cost tab:

- AWS EBS volumes whose state is `available`
- GCP Persistent Disk / Hyperdisk resources whose `users` field is empty
- `cost_unattached_block_volume_daily`
- `ci_job_state`

Preconditions:

- apply `029_create_cost_unattached_block_volume_daily.sql` to the dashboard database before the first run
- use the exact `ci-dashboard-jobs` image tag printed by the successful ee-apps release workflow `Generating tags...` step; do not use `latest`, do not guess from dates, and do not treat `git describe` as final
- confirm the jobs image exists in GHCR before applying ee-ops manifests, for example `docker manifest inspect ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag>`
- configure at least one scan target: `CI_DASHBOARD_AWS_EBS_REGIONS` or `CI_DASHBOARD_GCP_BLOCK_VOLUME_PROJECTS`
- grant AWS read access for `ec2:DescribeVolumes`; if `CI_DASHBOARD_AWS_EBS_ACCOUNT_ID` is omitted, the job also needs `sts:GetCallerIdentity`
- grant GCP read access for Compute Engine disks in each configured project; the job reads an access token from `CI_DASHBOARD_GCP_ACCESS_TOKEN` or from the pod metadata server
- keep the job suspended until the SQL migration and cloud credentials are confirmed

Render a CronJob manifest:

```bash
cd ci-dashboard
./scripts/render_unattached_block_volumes_cronjob.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-eq-prd-insight-db \
  --ca-secret ci-dashboard-backfill-ca \
  --service-account ci-dashboard \
  --aws-ebs-regions us-west-2 \
  --aws-ebs-account-id <aws-account-id> \
  --gcp-projects <gcp-project-id> \
  --suspend true \
  > /tmp/ci-dashboard-sync-unattached-block-volumes.yaml
```

Apply it:

```bash
kubectl apply -f /tmp/ci-dashboard-sync-unattached-block-volumes.yaml
```

Recommended first bring-up:

```bash
kubectl -n apps create job --from=cronjob/ci-dashboard-sync-unattached-block-volumes ci-dashboard-sync-unattached-block-volumes-smoke-$(date +%s)
kubectl -n apps logs job/<smoke-job-name> --follow
```

Useful overrides:

- `--schedule "20 3 * * *"` keeps the default once-per-day scan.
- `--aws-secret <secret>` injects `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` from a Kubernetes secret when runtime AWS identity is not available.
- `--aws-session-token-key <key>` also injects `AWS_SESSION_TOKEN` for temporary AWS credentials.
- `--aws-owner-tag-keys "owner,owner_email,github"` overrides the AWS owner tag parsing order.
- `--gcp-access-token-secret <secret>` injects `CI_DASHBOARD_GCP_ACCESS_TOKEN`; otherwise the pod must be able to read a metadata-server token, for example through GKE Workload Identity.
- `--gcp-owner-label-keys "owner,owner_email,github"` overrides the GCP owner label parsing order.
- `--concurrency-policy Forbid` avoids overlapping daily scans.
- `--suspend true` is recommended until the smoke job has written fresh rows.

Validation:

```bash
kubectl -n apps get cronjob ci-dashboard-sync-unattached-block-volumes
kubectl -n apps get jobs -l app.kubernetes.io/name=ci-dashboard-sync-unattached-block-volumes
kubectl -n apps logs job/<latest-job-name>
```

Database checks:

```sql
SELECT snapshot_date, vendor, COUNT(*) AS volumes
FROM cost_unattached_block_volume_daily
GROUP BY snapshot_date, vendor
ORDER BY snapshot_date DESC, vendor;
```

Notes:

- The job only writes the snapshot table and job state; actual cost is joined later from existing billing attribution rows.
- `sync-unattached-ebs-volumes` is kept for AWS-only debug/backfill. Production scheduling should use `sync-unattached-block-volumes` so AWS and GCP snapshots advance together.
- `CI_DASHBOARD_GCP_BLOCK_VOLUME_PROJECTS` falls back to `CI_DASHBOARD_GCP_PROJECT` when unset or empty. For an AWS-only CronJob, make sure the `envFrom` secret and pod environment do not define `CI_DASHBOARD_GCP_PROJECT`; an empty `--gcp-projects` value does not disable this fallback.
- Docker images copy `ci-dashboard/sql/`, but they do not apply migrations automatically. Apply SQL 029 through the existing database rollout path before unsuspending the CronJob.
