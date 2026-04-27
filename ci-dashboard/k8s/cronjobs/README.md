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
- Current cluster values we already verified:
  - Kafka bootstrap service: `cluster-cd-kafka-bootstrap:9092`
  - Jenkins service DNS for internal callers: `http://jenkins.jenkins.svc.cluster.local`

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
- `--gcs-prefix ci-dashboard/v3/jenkins-logs` keeps artifacts under one dedicated prefix.
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
