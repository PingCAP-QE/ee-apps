# Tencent utilization V2: opt-in operations

Implementation: [V2 design](resource-utilization-design-v2.md), especially §§14–17.
This PR does not run a migration, create a topic, deploy workloads, or enable
collection. All four generated CronJobs are suspended. The example profile is
intentionally unverified and cannot enable ingestion.

## Scope and data

Only Tencent CICD account `100050658403`; explicit regions, one cluster and CI
namespaces. Run dispatcher, Kafka and workers in Tencent, not via GCP brokers.
Cost Insight receives no Kubernetes token or RBAC. The existing CI Dashboard
watcher reads Kubernetes and exports approved Pod context; Node LIST only resolves
historical query selectors. No new usage sampler, person/disk reporting, or change
to billing and personal allocation.

V2 creates four tables instead of the rejected mixed-scope V1 tables:

- `machine_utilization_daily`: one machine/day, CPU and memory together, capacity
  integrals and individual peaks. One year queryable; no separate weekly table,
  account-class cache, or dedicated machine-cleanup service at the current scale.
- `pod_utilization_context`: active identity/requests/selected-label intervals and
  minimal planning progress; ended contexts are removed after bounded tail work.
- `utilization_task`: durable outbox, lease and completion checkpoint in one row.
  No duplicate raw response or result JSON. Kafka carries only `task_id`.
- `workload_utilization_daily`: bounded anonymous label-set/requests sufficient
  statistics, without Pod UID/name or group peaks. Retains current/previous
  Beijing week; deleting ended contexts does not erase already-produced business
  statistics. There are no additional fixed Job/repo rollup copies.

The unapplied `sql/031_create_utilization.sql` creates V2 tables only; no automatic
DROP/ALTER of old tables or existing cost/CI data. If someone independently ran a
V1 migration, inspect actual schema/state and review migration separately. Never
silently reinterpret a version-1 guard as V2.

## Before opting in

Validate in an isolated canary:

1. Historical billing ID/region/spec mapping and complete paging. Samples from
   2026-11-20 are proven against the billing API, **not a COS export object**.
   The bounded scan cap is 30,000 rows per product/day (300 pages of 100). The
   verified `p_011691` sample had 8,804 rows: 29.3% of the cap, with 21,196 rows
   of headroom (3.4x total capacity). Record each product/day's Total and remaining
   headroom in the canary; above-cap scans still fail closed without partial candidates.
2. Each source's 60-second average units, bucket timestamps, memory conversion,
   reporting delay, minimum replay horizon and safety margin. Five-day sample
   success is not a contractual fourteen-day guarantee.
3. Real TiDB pessimistic transactions, lease fencing, JSON SQL, cleanup indexes,
   concurrency and result-plus-checkpoint atomicity. Offline dialect compilation
   does not prove runtime TiDB behavior.
4. Tencent Kafka Ready/connectivity/TLS/SCRAM/ACLs, broker capacity and an isolated
   end-to-end topic test. Earlier GKE Kafka health is not Tencent runtime proof.
5. Actual candidate/label cardinality, API calls/bytes/QPS, job runtime, DB/index
   storage and RU, backlog and cleanup throughput. ¥100/month is a calibration
   budget, not measured cost or an enforced cloud billing cap.

## Configuration and credentials

Use a read-only Tencent identity for billing, instance-type catalogue, STS and
monitor APIs, supplied as `TENCENTCLOUD_SECRET_ID`, `TENCENTCLOUD_SECRET_KEY`, and
optional `TENCENTCLOUD_TOKEN`. Do not put values in arguments, manifests or logs.

Complete `config/tencent-utilization.example.json` with reviewed horizon/scales,
ISO verification time, evidence reference and timestamp-alignment confirmation.
`labels` is an explicit bounded list of approved non-personal business keys;
standard repo/job/test-project keys are defaults, not fixed schema columns.
Configure watcher `CI_DASHBOARD_UTILIZATION_LABEL_KEYS` to the same comma-separated
list. Empty labels and unknown requests remain valid; missing is not zero.

DB uses the existing Cost Insight database configuration. Kafka clients require:

| Variable | Value / source |
| --- | --- |
| `UTILIZATION_KAFKA_BOOTSTRAP_SERVERS` | Tencent internal broker endpoint, e.g. `cluster-cd-kafka-bootstrap.kafka.svc:9094` |
| `UTILIZATION_KAFKA_USERNAME` | Dedicated scoped KafkaUser name |
| `UTILIZATION_KAFKA_PASSWORD` | Secret reference, never inline |
| `UTILIZATION_KAFKA_CA_FILE` | Mounted cluster CA, e.g. `/etc/kafka/ca.crt` |
| `UTILIZATION_KAFKA_TOPIC` | Default `tencent-utilization-tasks` |
| `UTILIZATION_KAFKA_GROUP` | Default `tencent-utilization-workers` |

The standalone [topic/user template](../k8s/utilization/kafka.yaml) targets Tencent
namespace `kafka`, label `strimzi.io/cluster: cluster-cd`. It is not referenced by
auto-deploy configuration. Topic auto-creation is not assumed. Review the template
and provision through the Tencent GitOps process; map the operator-generated
credential and CA into the jobs namespace with the existing secret mechanism.
Do not reuse the Jenkins topic or broaden its existing consumer ACLs.

## Ordered activation

After review and explicit production approval:

1. Apply the reviewed V2 migration to the correct DB; keep producers off.
2. Prepare profile, scoped Kafka topic/user/CA/secret references and watcher Node
   read-only binding. No Kubernetes access is granted to Cost Insight.
3. Initialize the disabled registry, then run independent maintenance:

   ```bash
   cost-insight utilization-configure --profile /etc/utilization/profile.json \
     --start-date YYYY-MM-DD
   cost-insight utilization-maintain
   ```

4. Render (do not apply automatically) the suspended Tencent workloads:

   ```bash
   python scripts/render-utilization-cronjobs.py \
     --namespace ee-cd --image IMAGE \
     --db-secret DB_SECRET --credential-secret CLOUD_SECRET \
     --profile-configmap PROFILE_CONFIGMAP \
     --kafka-secret KAFKA_ENV_SECRET --kafka-ca-secret KAFKA_CA_SECRET
   ```

   Schedules: maintenance every15min; historical machine planning daily06:30;
   dispatcher and bounded worker batches everyminute, all Asia/Shanghai.
   The watcher and API opt-in remains `CI_DASHBOARD_UTILIZATION_ENABLED=true`.

5. After healthy cleanup/statistics and source canaries, explicitly configure the
   same registry with `--enable --confirm-source-horizons`. Unsuspend maintenance
   first, then reviewed producers/workers. This document does not authorize that
   operation or a PR merge.

## Commands and recovery

```bash
# Plan an explicit historical machine date; no live inventory polling.
cost-insight sync-tencent-utilization --profile PROFILE --date YYYY-MM-DD --dry-run
cost-insight sync-tencent-utilization --profile PROFILE --date YYYY-MM-DD

# No per-Pod timers: dispatcher plans confirmed ten-minute windows and due tails.
cost-insight utilization-dispatch --profile PROFILE --limit 100
cost-insight utilization-worker --profile PROFILE --limit 100
cost-insight utilization-maintain --dry-run
```

Without `--date`, the machine planner scans D-4 and D-3, subject to the verified
horizon. A successful bounded bill scan precedes task creation. Machine planning
`--dry-run` checks source reachability/contracts and counts candidate windows only;
it does **not** validate enqueue scope, admission, expiry or existing-window
snapshot/overlap conflicts. Configure/maintenance dry runs only return registry
state. Dry-run success is not proof that the real planner will enqueue successfully;
verify those invariants with an enabled, isolated canary registry before production.
Source windows are
at most30min; Pod windows are10min with10min reporting allowance. Short Pods still
get a tail window for confirmed whole minutes. Selector evidence is refreshed by
one shared, bounded Node LIST every minute when utilization is enabled. A new
binding requires later Node evidence and a Node that predates the assignment;
no bare IP fallback is used. Canary must measure Kubernetes LIST volume,
selector confirmation delay and missing-selector rates, especially for short Pods.

Tasks, daily contributions and completion use the shared V2 guard and DB clock.
Lease/deadline fencing rejects stale workers. Broker-confirmed publish precedes
marking dispatched; DB result/done commit precedes offset acknowledgement. Failed
attempts retry with backoff capped at five minutes until the fixed task deadline;
there is no attempt-count expiry. Maintenance records missing usage at expiry.
Crash replays may repeat messages or an unfinished source window but not its committed
contribution. A conflicting completed snapshot is rejected, not silently added as
a second version; do not clear done checkpoints or invent an ad-hoc repair.
Machine planning reports per-candidate snapshot/overlap validation failures in each
usage day's `errors` list (machine ID, region, reason), while continuing unrelated
machines and days. Skipped days or nonempty `errors` make the CLI exit 2; the
affected candidate's transaction is rolled back. Investigate and retry the affected
date with `--date` before its verified source horizon expires; the default D-4/D-3
scan does not automatically revisit older failed dates. The V2 planner also
queries by requested usage day; it can miss late charges found only in a different
`BillDay` partition. A complete scan for one request is not proof of complete
usage-day population. The partition/usage-day separation is tracked in the V3 design.

Ended contexts are removed by maintenance only after all confirmed whole minutes
are finalized and their tasks are drained. Orphaned watcher intervals close at
their last confirmation after15min; closed work has a24h evidence/deadline bound.
If a context expires before the dispatcher plans its entire tail, maintenance
adds missing expected workload statistics directly for the **unplanned** whole
minutes, with no fabricated observations and no overlap with planned tasks.
Statistics and planning-cursor advancement commit atomically under the shared
guard, even while collection is paused; dispatcher recovery is not required.
Windows remain ten-minute-aligned and split at Beijing midnight. Dates outside
anonymous workload retention (or before the configured start date) are advanced
past without recreating results. Existing pending-task expiry remains separate.

Maintenance commits small batches: at most `batch_size` context rows and
`batch_size` unplanned-tail windows per transaction (`batch_size` defaults to
1000, maximum1000), with 50,000 deleted rows or5min per run. Contexts are visited
once per run; a tail exceeding the shared window budget remains for later
maintenance calls, without repeating its committed contribution. Keep maintenance
running while paused until this workset drains. No permanently retained DLQ, full
Pod export, raw-minute archive or object-store quarantine.

Watch backlog/oldest due task, `collection_failed` quality gaps, source errors, per-metric
coverage/unknown denominator, retained rows, table+index bytes, maintenance time
and pause reasons (`row_budget`, `byte_budget`, `bytes_unreliable`, `cleanup_backlog`).
The workload API flags a selected date range as partial while the account has
unsettled Pod windows or confirmed unplanned evidence in the range; this is a
conservative warning across label filters. Coverage then describes settled windows
only. Machine status expects the full D-3 Beijing day from the latest
daily06:30 planner, with a one-hour dispatch/collection allowance; historical data
is not stale merely because its cutoff is more than eight hours old.
Current warning1M rows/4GiB (reported in maintenance output's `warnings` list);
pause2M/8GiB or stale/unreliable maintenance/statistics after4h. All owned tables count toward the budget, including
machine facts. A paused producer does not stop maintenance. Disable collection via
`utilization-configure` without `--enable`; keep cleanup running until the active
workset is drained. API disabled/missing-schema states must not break cost pages.
