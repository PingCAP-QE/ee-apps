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
