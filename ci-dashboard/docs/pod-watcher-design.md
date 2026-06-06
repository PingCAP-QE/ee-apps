# CI Dashboard Pod Watcher Design

Status: Draft v0.1

Last updated: 2026-05-01

## 1. Problem

The current pod ingestion path has two incomplete sources: Cloud Logging covers
more history but lacks full Pod metadata and true Pod creation time, while ad hoc
Kubernetes API lookup has full metadata but misses short-lived Pods after they
are deleted. Cloud Logging `Created` is a container-created event, not Pod
`metadata.creationTimestamp`, so scheduling wait must not use it as Pod creation
time.

## 2. Goal

Run a long-lived watcher inside the cluster so CI Dashboard sees Pods while they are alive.

The watcher should:

- watch target namespaces for Pod `ADDED` and `MODIFIED` events
- persist full Pod labels, annotations, uid, and `metadata.creationTimestamp`
- watch Kubernetes Events for lifecycle reasons such as `Scheduled`, `Pulling`, `Pulled`, `Created`, `Started`, `ErrImagePull`, and `ImagePullBackOff`
- upsert existing `ci_l1_pod_events` and `ci_l1_pod_lifecycle` rows idempotently
- keep the dashboard's scheduling wait definition strict: `scheduled_at - pod_created_at`

## 3. Non-Goals

- no dashboard UI changes in this slice
- no invented historical Pod creation time from Cloud Logging
- no Jenkins build ingestion or error-classification replacement
- no backfill for Pods deleted before watcher rollout

## 4. Data Contract

The source of truth for Pod creation time is Kubernetes Pod `metadata.creationTimestamp`.

The source of truth for lifecycle milestones is Kubernetes Event reason timestamps:

- `Scheduled`: scheduler accepted the Pod onto a Node
- `Pulling` / `Pulled`: image pull start and completion
- `Created` / `Started`: container creation and start, not Pod creation
- `FailedScheduling`: intermediate retry signal, not final CI failure
- `ErrImagePull` / `ImagePullBackOff`: image pull failure/backoff signal

Rows remain keyed by the existing lifecycle identity:

```text
(source_project, namespace_name, pod_uid, pod_name)
```

The watcher writes the existing tables:

- `ci_l1_pod_events`
- `ci_l1_pod_lifecycle`
- `ci_job_state`

## 5. Runtime Shape

The watcher is a Deployment, not a CronJob.

Recommended first deployment:

- namespace: `apps`
- replicas: `1`
- service account: `ci-dashboard`
- target namespaces: `prow-test-pods,jenkins-tidb,jenkins-tiflow`
- RBAC: `get`, `list`, `watch` on `pods` and `events` in target namespaces

On startup, each namespace is listed once and then watched from the returned
`resourceVersion`. If the stream disconnects or the resource version expires,
the worker relists and resumes.

Rollout precondition: migration `017_alter_ci_l1_pod_lifecycle_add_pod_created_at.sql`
must be applied before `watch-pods` starts, otherwise metadata writes will fail because the
watcher persists Pod `metadata.creationTimestamp` into `ci_l1_pod_lifecycle.pod_created_at`.

## 6. Runtime Configuration

Required:

- `CI_DASHBOARD_GCP_PROJECT` or `CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT`: source project stored in pod tables.
- `CI_DASHBOARD_POD_EVENT_NAMESPACES`: comma-separated namespaces to watch.

Recommended defaults:

- `CI_DASHBOARD_POD_WATCH_TIMEOUT_SECONDS=300`
- `CI_DASHBOARD_POD_WATCH_RETRY_DELAY_SECONDS=5`
- `CI_DASHBOARD_POD_WATCH_HEALTH_PORT=8081`
- `CI_DASHBOARD_POD_WATCH_STALE_AFTER_SECONDS=720`
- `CI_DASHBOARD_JENKINS_POD_NAME_PREFIX_CACHE_SECONDS=900`

Optional metadata:

- `CI_DASHBOARD_KUBERNETES_CLUSTER_NAME`
- `CI_DASHBOARD_KUBERNETES_LOCATION`

## 7. Health And Self-Healing

The watcher exposes `/livez` and `/readyz` from the same process. Each watched
namespace registers two heartbeat streams, `<namespace>/pods` and
`<namespace>/events`. Heartbeats update after each successful list, watch
response, and persistence batch. If any stream is stale beyond
`CI_DASHBOARD_POD_WATCH_STALE_AFTER_SECONDS`, health checks fail and Kubernetes
restarts the Pod.

Recommended first probe settings:

- health port: `8081`
- watch timeout: `300s`
- stale-after: `720s`
- startup probe: `/livez`, up to 3 minutes
- liveness probe: `/livez`
- readiness probe: `/readyz`

## 8. Dashboard Semantics

Scheduling wait should only use rows where:

- `pod_created_at IS NOT NULL`
- `scheduled_at IS NOT NULL`
- `scheduled_at >= pod_created_at`

`FailedScheduling` should not be displayed as a CI failure rate. In autoscaled
GKE it is often an intermediate retry signal. The dashboard should emphasize:

- final unscheduled Pods, if any
- scheduling wait distribution
- image pull latency distribution
- image pull error/backoff counts

## 9. Failure Handling

Writes are idempotent:

- pod events are deduped by `(source_project, source_insert_id)`
- lifecycle rows are upserted by `(source_project, namespace_name, pod_uid, pod_name)`

The worker records job state under `ci-watch-pods`. Restart is safe because
writes are idempotent and startup relist refreshes current Pod metadata. Watch
`410 Gone` / expired resource-version events relist immediately; other watch
errors reconnect after the retry delay. Watch streams are not a durable
historical log, so Pods created and deleted while the watcher is down can still
be missed.

## 10. Rollout Plan

1. Deploy with `replicas=1`.
2. Validate fresh rows have `pod_created_at`, labels, annotations, and `Scheduled`.
3. Compare watcher rows against existing Cloud Logging rows for a recent window.
4. Update dashboard cards to count scheduling wait only for complete watcher-backed rows.
5. Keep `sync-pods` temporarily, but never use Cloud Logging `Created` as Pod creation time.

## 11. Open Follow-Ups

- add a freshness monitor for latest `metadata_observed_at` by namespace
- add durable watch state only if startup relist is not precise enough
