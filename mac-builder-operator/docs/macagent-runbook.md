# MacAgent operations runbook

This runbook covers the cluster scheduler, `MacAgent` inventory and worker
maintenance. It assumes the chart is deployed with `controller.macAgent=true`,
`controller.scheduler=true`, and `worker.create=true`.

## Verify the fleet

```bash
kubectl get macagents
kubectl describe macagent <name>
kubectl get macbuilds -n ee-cd
```

A healthy worker is `Phase=Online` with `Ready=True` and a recent
`LastHeartbeatTime`. `kubectl describe` shows OS version/build, architecture,
CPU model/cores, memory, disk, load averages, conditions and assigned builds.

## Why a build is Pending

`kubectl describe macbuild <name> -n ee-cd` shows `Status.PhaseMessage`, for
example:

- `no MacAgents registered` — no worker has registered.
- `no schedulable MacAgent (...)` — check `Phase`, `Ready`, `spec.unschedulable`,
  `spec.drain`, capacity, `workerSelector`, and disk pressure.
- `agent <name> did not start the build ... reassigning` — the assignment lease
  expired; the build returns to `Pending` and is retried up to
  `--max-assignment-attempts`.

## Build logs

```bash
kubectl macbuild logs <build> -n ee-cd
kubectl macbuild logs <build> -n ee-cd -f --tail 200
```

- `404` means the build has no uploaded log (an old build, the log server is
  disabled, or the agent has not uploaded its first chunk).
- `403` means the account lacks `get` on `services/proxy`. Use
  `kubectl port-forward svc/mac-builder-operator-logs 8080:8080 -n ee-cd` and
  pass `--server http://127.0.0.1:8080`.
- The agent keeps a local copy under its `--log-dir`; set `--log-upload=false`
  on clusters without the log server.
- `kubectl describe macbuild` shows `Status.Logs.Command`, `SizeBytes` and
  `Complete`.

The log Service is plain HTTP inside the cluster. API server proxy requests are
authenticated and TLS-protected by the API server, but any pod that can reach
the ClusterIP can read logs. Restrict the namespace with a NetworkPolicy, size
the PVC for `logServer.maxStoreBytes`, and avoid writing credentials to build
output; common GitHub token shapes are redacted, but redaction is best-effort.
The agent retries finalization three times (`--log-finalize-timeout`, default
10s per attempt) and keeps the local copy if the service stays unavailable.

## Offline and reconnect

- 60s without a heartbeat marks the agent `Offline` (`Ready=False`,
  reason `HeartbeatLost`).
- Builds in `Preparing`/`Building`/`Publishing` keep a grace period
  (`spec.offlineGracePeriod`, default 10m). The phase message shows the failure
  deadline.
- If the agent returns within the grace period, the build continues.
- After the grace period the build becomes `Failed` with reason `AgentLost` and a
  message containing the agent, last heartbeat and last phase. The failure is
  not retried automatically; retrigger the build through tibuild-v2/Tekton.

## Disk pressure

When `status.runtime.diskFreeBytes` drops below `spec.minFreeDiskBytes`, the
agent sets `DiskPressure=True` and the scheduler stops assigning new builds.
The condition clears automatically when space is freed. A build that reaches
the worker with insufficient disk fails before cloning with an `insufficient
disk` message; free space and retrigger the build.

`DiskUnknown` means the worker could not read `df`; it also keeps the agent out
of scheduling. The agent retries with `--disk-check-attempts` (default 2) and
`--disk-check-retry-interval` (default 100ms) before failing a build with
`disk metrics unavailable`. Check the agent logs and the raw command, then
restart the LaunchAgent after fixing the runner:

```bash
ssh mac-worker 'df -k /; launchctl print gui/$(id -u)/net.pingcap.macbuild-agent | head -40'
```

Change the threshold per worker or for the whole cluster:

```bash
# Worker-local override (installer).
./deploy/macos-worker/install.sh --min-free-disk-bytes 53687091200

# New agents created by the chart.
helm upgrade mac-builder-operator charts/mac-builder-operator \
  --set agent.minFreeDiskBytes=53687091200
```

To recover:
1. Free space on the worker (`df -h /`, remove stale `macbuild-*` temp dirs).
2. Wait for the next heartbeat (20s) and confirm
   `kubectl describe macagent <name>` shows `DiskPressure=False`.
3. Retrigger the failed MacBuild.

### Metrics

The operator exposes:

- `macbuilder_agent_ready{agent}` — 1 when the agent is ready.
- `macbuilder_build_assignment_total{result}` — assignment outcomes
  (`assigned`, `reassigned`, `failed`, `no_candidate`).
- `macbuilder_agent_lost_total` — builds failed after the offline grace.
- `macbuilder_workspace_cleanup_failures_total` — stale workspace directories
  that could not be removed.

## Graceful shutdown

On SIGTERM the agent waits up to `--shutdown-grace` (default 10m) for active
builds to finish before stopping the manager. If the grace expires, the build is
interrupted and the persisted state causes a rerun (Preparing/Building) or a
failure (Publishing) on the next start. Prefer cordon/drain for planned
maintenance.

## Cordon, drain and upgrade

```bash
# Stop new assignments; active builds continue.
kubectl patch macagent <name> --type=merge -p '{"spec":{"unschedulable":true}}'

# Wait for active builds to finish.
kubectl get macagent <name> -o jsonpath='{.status.activeBuilds}'

# Stop the worker.
launchctl bootout gui/$(id -u) net.pingcap.macbuild-agent
```

Install the new agent binary with the installer, start it, verify `Phase=Online`
and the new `status.agentVersion`, then uncordon:

```bash
kubectl patch macagent <name> --type=merge -p '{"spec":{"unschedulable":false}}'
```

`spec.drain=true` is the same as cordon but keeps the phase `Draining` until
`activeBuilds=0`, then `Drained`. It never kills a running build. To force a
worker offline, stop the agent and use the normal `AgentLost` path.

## Stop-the-world cutover (Phase 1 only)

The agent protocol changed from self-claim to controller-assigned builds. The
first rollout is a maintenance window:

1. Announce the window and pause new macOS builds.
2. Stop all LaunchAgents on the workers.
3. Deploy the new controller/chart (CRDs, RBAC, MacAgent/scheduler flags).
4. Reinstall the agent on every worker with a fixed `--eeapps-ref`.
5. Verify `kubectl get macagents` shows every worker `Online`.
6. Run a small `push=false` smoke build and verify
   `Assigned -> Preparing -> Building -> Publishing -> Succeeded`.
7. Resume builds.

Rollback: roll the chart back, reinstall the old agent binary, and reset any
builds still in `Assigned` to `Pending` (only builds that have not started).
The old agent ignores `Assigned` builds, so leaving them would strand them.
