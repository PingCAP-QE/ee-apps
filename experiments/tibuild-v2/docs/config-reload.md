# Configuration reload

`cmd/tibuild` and `cmd/tibuild-worker` poll their mounted configuration file and
apply a change without a restart. This page lists which keys take effect that
way, which ones still need a rollout, and how to verify or roll back a change.

The file is `config.yaml` of the `v2-config` Secret (API) or the
`config-*` Secret of a worker Deployment. Kubernetes refreshes Secret mounts by
swapping the `..data` symlink, which does not produce a reliable event on the
mounted file, so the process compares the file content every **15s** instead of
watching it.

## Hot keys

A change to any of these takes effect within one poll interval.

| Key | Effect |
| --- | --- |
| `publish.nightly.enabled` | switch convergence on or off |
| `publish.nightly.tick_interval` | reconciled immediately, not after the previous tick |
| `publish.nightly.batch` | claim size per tick |
| `publish.nightly.default.*` | window, anchor, timezone, retry_after, jitter, on_failure for unlisted targets |
| `publish.nightly.targets.*` | per-target window/anchor/timezone/retry_after/jitter/on_failure, and the set of converging targets |
| `publish.delivery_config_path` | the planner re-points at the new rules file |
| `publish.tiup_mirrors` | mirror base URLs used by new plans |
| `publish.tiup_dependencies` | `depends_on` seeding for new units |
| `worker.metrics_interval` | task stats and alert evaluation period |
| `worker.reap_interval` | expired-lease reclaim period |
| `worker.backoff_base`, `worker.backoff_max` | retry delay of failed steps |
| `worker.alerts.*` | alert thresholds and recipients |
| `lark.channels` | notification recipients |
| `image_sync.source_regx`, `image_sync.target_regx` | image sync request validation |
| `github.*`, `jenkins.*`, `tekton.*` (except `reconciler_interval`), `product_repo_map`, `image_mirror_url_map` | devbuild/hotfix behaviour |

`docs/publish.md` and the README describe what each key means; this page only
says whether it is hot.

## Keys that require a restart

A change to one of these rejects the **whole document**: the process keeps the
previous configuration and logs the field names. It does not apply the hot keys
of the same edit, so behaviour never depends on which fields happen to be hot.

| Key | Why |
| --- | --- |
| `store` | the connection pool is built once |
| `worker.id` | the identity written to `claimed_by` |
| `worker.kinds` | decides which claim loops exist |
| `worker.targets`, `publish.targets` | decide the queue shard a worker claims |
| `worker.concurrency` | the number of claim loops |
| `worker.poll_interval` | the claim loop interval |
| `worker.lease`, `worker.heartbeat`, `worker.hard_timeout`, `worker.max_attempts` | in-flight lease and fencing semantics |
| `publish.tiup_binary` | the executable used to publish |
| `identity_headers` | the HTTP middleware is built at startup |
| `tekton.reconciler_interval` | the scheduler job is created at startup |
| `lark.app_id`, `lark.app_secret` | credentials; rotate them with a rollout |

The list lives in `pkg/config` (`DefaultRestartOnlyFields`, plus
`ExtraRestartOnlyFields` in `cmd/tibuild`), and anything not listed there is
treated as a hot key only if a consumer reads it per call — new keys default to
restart-only until the code says otherwise.

## What a reload looks like

Every attempt emits one structured log line. Field **paths** are logged; values,
including secrets, never are.

```text
config loaded generation=1 changed=[]
config reload applied generation=2 changed=[publish.nightly.targets.tiup-staging.window worker.metrics_interval]
config reload unchanged generation=2
config reload rejected generation=2 candidate=3 error="invalid nightly.targets.tiup-staging.window \"0s\": must be a positive duration"
config reload ignored generation=2 candidate=3 reason=restart_required fields=[worker.targets]
config reload deferred generation=2 error="read ...: no such file or directory"
```

| Line | Meaning |
| --- | --- |
| `applied` | the revision is live; `changed` lists the dotted paths that differ |
| `unchanged` | the file content hash is identical (debug level) |
| `rejected` | the document did not parse or did not validate; the previous revision keeps running |
| `ignored` | a restart-only field changed; the whole document was rejected |
| `deferred` | the file could not be read (swap window, broken mount); retried on the next poll |
| `still rejected, document unchanged` | the same rejected document is still mounted; logged at debug so a forgotten bad edit does not fill the log every poll |

A rejected document is retried on every poll: the counters keep counting attempts
(so a rate alert still fires), but the error line is emitted only when the
document or the verdict changes.

## Metrics

The watcher exports:

| Metric | Meaning |
| --- | --- |
| `config_reload_total{result}` | `success`, `unchanged`, `invalid`, `restart_required`, `read_error` |
| `config_generation` | revision currently applied (starts at 1) |
| `config_last_reload_timestamp_seconds` | when the applied revision was loaded |
| `config_restart_required_total{field}` | rejected documents per restart-only field |

The worker serves them on its health port (`/metrics`, 8081) and the chart's
`worker.podMonitor` scrapes it. The API serves them only when `-metrics-addr` is
set (chart value `server.metricsPort`); the listener is pod-only, so `/metrics`
is never routed through the ingress. Set `server.podMonitor.enabled=true` where
the cluster has the Prometheus Operator CRDs.

Alert on `config_reload_total{result="invalid"}` or
`{result="restart_required"}` increasing: both mean an edit did not take effect.

## Verifying a change

```bash
# 1. what revision is the process running? (prod2 runs the API plus one worker
#    Deployment per publish shard; adjust the name for the cluster you are in)
kubectl -n tibuild logs deploy/v2-tibuild-v2 --since=10m | grep 'config'
kubectl -n tibuild logs deploy/v2-tibuild-v2-worker-tiup-staging --since=10m | grep 'config'

# 2. counters, without a Prometheus
kubectl -n tibuild port-forward deploy/v2-tibuild-v2-worker-tiup-staging 8081:8081
curl -s localhost:8081/metrics | grep '^config_'

# 3. the pod must not have restarted
kubectl -n tibuild get pods -l app.kubernetes.io/name=v2-tibuild-v2 \
  -o custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount,AGE:.metadata.creationTimestamp
```

A hot change is verified by the `config reload applied` line plus the behaviour
it controls (for example `nightly target schedule` for a window change). See
`docs/publish.md` for the nightly schedule logs.

## Rolling back a bad change

1. **Rejected document** (`rejected` / `ignored`): nothing was applied, so there
   is nothing to roll back. Fix the file; the next poll picks it up. The
   `candidate` in the log tells you which edit was refused.
2. **Applied but wrong** (`applied`): restore the previous value in the Secret
   (or in the ExternalSecret template in `ee-ops`). The next poll applies it, and
   the log shows the reverse `changed` list.
3. **A value that only a restart can fix** (for example a bad `worker.lease`):
   the document is rejected by design; rollout the Deployment once the file is
   corrected, otherwise the field never takes effect.

Never edit the mounted file inside a running pod: it is a read-only Secret mount
and the change is lost on the next sync. Edit the Secret's source (the
ExternalSecret template in `PingCAP-QE/ee-ops`) and let External Secrets update
the Secret.
