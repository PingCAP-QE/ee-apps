# CI Dashboard V2 Pod Rollout

Status: Draft v0.3

Last updated: 2026-04-21

Scope:
- rollout for `sync-pods`
- no dashboard UI publish in this document
- no Jenkins-log collection in this document

Related files:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/src/ci_dashboard/jobs/sync_pods.py`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/scripts/render_sync_pods_cronjob.sh`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/k8s/cronjobs/README.md`

## 1. Goal

Safely bring up recurring pod-event ingestion and inline build linkage on the prow GKE cluster without stressing the kube-apiserver and without changing any source table owned by other applications.

All writes stay limited to project-owned tables:
- `ci_l1_pod_events`
- `ci_l1_pod_lifecycle`
- `ci_job_state`

## 2. Preconditions

Before rollout, all of the following should be true:

- the target database already contains:
  - `ci_l1_builds`
  - `ci_job_state`
  - `ci_l1_pod_events`
  - `ci_l1_pod_lifecycle`
- the jobs image containing `sync-pods` has been built and pushed
- the Kubernetes runtime identity used by the job can read Cloud Logging for the target project
- DB access secret is already available
- SSL CA secret is available when the TiDB instance requires it

Observed canary notes on 2026-04-21:
- the default `ci-dashboard` runtime identity did not have enough Cloud Logging permission
- manual smoke validation succeeded only with a temporary explicit `CI_DASHBOARD_GCP_ACCESS_TOKEN`
- do not enable recurring mode until workload identity or an equivalent production-safe permission path is in place
- separate manual verification proved Jenkins GCP pod rows can be linked from pod annotations and labels without using Jenkins console lookup
- Jenkins namespace coverage and Kubernetes pod metadata access are release requirements for V2.1, not optional later refinements

## 3. Recommended Runtime Shape

Recommended namespace:
- `apps`

Recommended schedule:
- every 10-15 minutes

Recommended first resource profile:
- request: `500m` CPU, `1Gi` memory
- limit: `2` CPU, `4Gi` memory

Recommended behavior:
- `concurrencyPolicy: Forbid`
- start suspended for the first apply
- use manual canary Job before enabling the schedule

Recommended default namespace scope:
- `prow-test-pods`
- `jenkins-tidb`
- `jenkins-tiflow`

## 4. Render The CronJob Manifest

Example:

```bash
cd /Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard
./scripts/render_sync_pods_cronjob.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-db \
  --ca-secret ci-dashboard-ca \
  --gcp-project pingcap-testing-account \
  --service-account ci-dashboard \
  --suspend true \
  > /tmp/ci-dashboard-sync-pods.yaml
```

Key flags:
- `--gcp-project`: required
- `--service-account`: recommended so the job runs with the intended workload identity
- `--pod-event-namespaces`: use explicit event namespaces such as `prow-test-pods,jenkins-tidb,jenkins-tiflow`
- `--lookback-minutes`: first smoke run can be wider than recurring mode
- `--overlap-minutes`: default overlap reread buffer
- `--max-pages`: safety cap for one run

## 5. Apply In Suspended Mode

```bash
kubectl apply -f /tmp/ci-dashboard-sync-pods.yaml
kubectl -n apps get cronjob ci-dashboard-sync-pods
```

Expected state:
- CronJob exists
- `suspend: true`

## 6. Run A Canary Job

Create and run a one-off `sync-pods` Job, then validate:
- `prow-test-pods` rows are ingested
- `jenkins-tidb` and `jenkins-tiflow` rows are ingested
- sampled Jenkins pod rows parse to the expected build URL key
- multi-pod Jenkins builds are preserved as multiple pod lifecycle rows

Create a one-off Job from the CronJob template:

```bash
kubectl -n apps create job --from=cronjob/ci-dashboard-sync-pods ci-dashboard-sync-pods-smoke-$(date +%s)
```

Check status and logs:

```bash
kubectl -n apps get jobs -l app.kubernetes.io/name=ci-dashboard-sync-pods
kubectl -n apps logs job/<smoke-job-name> --follow
```

Expected signals:
- non-zero `source_rows_scanned` when there are recent pod events
- non-zero `event_rows_written` on the first successful run
- non-zero `pods_touched` when matching namespaces are active
- job exits successfully

Observed on 2026-04-21 for `sync-pods`:
- first smoke job failed because the initial image was arm64-only
- second smoke job reached the container but failed with Cloud Logging `403 PERMISSION_DENIED`
- third manual smoke job succeeded after injecting a temporary `CI_DASHBOARD_GCP_ACCESS_TOKEN`
- successful smoke populated `ci_l1_pod_events`, `ci_l1_pod_lifecycle`, and `ci_job_state`
- fourth manual smoke used image `us-docker.pkg.dev/pingcap-testing-account/internal/test/ci-dashboard-jobs:sync-pods-canary-20260421-173923`
- the fourth smoke reran a full `180m` window after clearing the `ci-sync-pods` watermark
- row counts after the fourth smoke:
  - `ci_l1_pod_events`: `86786`
  - `ci_l1_pod_lifecycle`: `3087`
- linkage coverage after the fourth smoke:
  - `PROW_NATIVE`: `232 / 304` = `76.32%`
  - `JENKINS`: `1573 / 2713` = `57.97%`
- Jenkins linkage improved materially versus the previous successful smoke (`1169 / 2428` = `48.15%`)
- newly linked live samples included `gcap-ticdc-pull-cdc-mysql-integration-light-next-gen-2164-*`
- remaining unlinked rows were still dominated by:
  - build-side lag (`ci_l1_builds` only reached `2026-04-21 09:00:00` while the pod watermark reached `2026-04-21T09:42:37.871069Z`)
  - opaque pod families such as `dm-it-*` that need annotation-based linkage

## 7. Smoke Validation Gates

Before enabling the recurring schedule, validate:

1. `ci_l1_pod_events` contains fresh rows for the target project.
2. `ci_l1_pod_lifecycle` is populated for a plausible subset of touched pods.
3. sampled Jenkins pod rows parse to the correct build URL key and match the expected build.
   The check must be based on pod annotations/labels, not inferred from pod name.
4. linkage from lifecycle rows to `ci_l1_builds` is plausible for both Prow-native and Jenkins rows.
5. `ci_job_state.job_name = 'ci-sync-pods'` shows a successful watermark.
6. rerunning the same job does not create duplicate event rows.

Use:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/docs/ci-dashboard-v2-pod-validation-plan.md`

## 8. Enable The Recurring Schedule

After the smoke run passes:

```bash
./scripts/render_sync_pods_cronjob.sh \
  --image ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:<tag> \
  --db-secret ci-dashboard-db \
  --ca-secret ci-dashboard-ca \
  --gcp-project pingcap-testing-account \
  --service-account ci-dashboard \
  --suspend false \
  > /tmp/ci-dashboard-sync-pods.yaml

kubectl apply -f /tmp/ci-dashboard-sync-pods.yaml
kubectl -n apps get cronjob ci-dashboard-sync-pods
```

Hard gate before this step:
- recurring mode must not be enabled while the runtime still depends on a temporary manual access token
- the `ci-dashboard` workload identity must be able to call Cloud Logging directly
- the `ci-dashboard` workload identity must be able to list/get Jenkins agent pods in target namespaces
- Jenkins metadata-based linkage must have acceptable coverage on sampled job families

## 9. Controlled Catch-Up

If the first production run starts late and historical log retention still covers the missing window, do a controlled catch-up by temporarily widening:
- `--lookback-minutes`
- `--max-pages`

Recommended approach:
- keep the CronJob suspended
- create one or more manual Jobs with a larger lookback
- validate progress after each run
- shrink the lookback back to steady-state operating values before enabling recurring mode

This keeps catch-up explicit and reduces the chance of unexpectedly large scans.

## 10. Failure Recovery

If a run fails:

1. inspect the Job logs
2. inspect `ci_job_state.last_error`
3. fix auth / DB / query parameters
4. rerun the smoke Job

Because the job uses:
- event-level unique keys
- lifecycle upsert
- watermark overlap reread

it is safe to rerun after a failure.

## 11. What This Rollout Does Not Yet Cover

- pod status/spec snapshot ingestion
- richer pod-derived timing fields beyond the current event-derived subset
- full Jenkins console-log collection or failure classification
- any UI enablement decision
