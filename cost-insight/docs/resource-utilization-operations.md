# Tencent utilization V3: opt-in operations

Implementation: [V3 design](resource-utilization-design-v3.md). The initial version
uses only Tencent billing detail reads for discovery; importing discovery evidence
from Cost Insight billing is a separate, gated cutover.
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

V3 retains four tables:

- `machine_utilization_daily`: one machine/Beijing day; per metric sum/count/peak
  of observed Tencent minute percentages. Missing metrics are not zero. No row
  for an all-missing day, machine specifications, or capacity/group metrics.
- `pod_utilization_context`: immutable query identity/requests/label snapshots for
  confirmed intervals, with a planning cursor and bounded tail. A missing cloud
  instance ID never falls back to an IP query.
- `utilization_task`: durable outbox, lease and completion checkpoint. Pod tasks
  refer to retained contexts instead of duplicating snapshots. Kafka carries
  `task_id` only. No raw responses, result JSON, or attempt history.
- `workload_utilization_daily`: per-date approved label combinations, not request
  buckets or Pod history. Matching usage/request integrals give a weighted mean;
  matched/expected Pod-minutes give time coverage, not capacity coverage. Only
  current and previous Beijing weeks are retained. Small label groups should not
  be called anonymous; use stable, approved non-personal values.

The unapplied `sql/031_create_utilization.sql` creates the V3 tables. No DROP/ALTER
or automatic migration of existing data. If a prior version was independently
applied, inspect schema and state and review a separate migration; do not enable
against a V1/V2 schema or reinterpret its registry.

## Before opting in

Validate in an isolated canary:

1. Historical billing ID/region mapping, charge intervals and complete paging. Samples from
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

Use a read-only Tencent identity for billing, STS and monitor APIs, supplied as `TENCENTCLOUD_SECRET_ID`, `TENCENTCLOUD_SECRET_KEY`, and
optional `TENCENTCLOUD_TOKEN`. Do not put values in arguments, manifests or logs.

Complete `config/tencent-utilization.example.json` with reviewed horizon/scales,
ISO verification time, evidence reference and timestamp-alignment confirmation.
`labels` is an explicit bounded list of approved non-personal business keys;
standard repo/job/test-project keys are defaults, not fixed schema columns.
Configure watcher `CI_DASHBOARD_UTILIZATION_LABEL_KEYS` to the same comma-separated
list. Use stable Repo/Job values, not person or run identifiers. Known identity
keys such as `pod_uid`, `build_id` and `test_execution_id` are rejected; do not
put instance or run identifiers in values of otherwise allowed keys. Empty
labels and unknown requests remain valid; missing is not zero. `pod_collection_enabled`
defaults to false, independently of machine collection. A machine-only profile
can omit Pod metric settings.

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

1. Apply the reviewed V3 migration to the correct DB; keep producers off.
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
   Watcher Pod export also requires the configured V3 registry to opt in.

5. After healthy cleanup/statistics and source canaries, explicitly configure the
   same registry with `--enable --confirm-source-horizons`. Unsuspend maintenance
   first, then reviewed producers/workers. This document does not authorize that
   operation or a PR merge.

## Commands and recovery

```bash
# Replay the original BillDay partitions together (not usage dates).
cost-insight sync-tencent-utilization --profile PROFILE --bill-day 2026-11-21 --bill-day 2026-11-22 --dry-run
cost-insight sync-tencent-utilization --profile PROFILE --bill-day 2026-11-21 --bill-day 2026-11-22

# No per-Pod timers: dispatcher plans confirmed ten-minute windows and due tails.
cost-insight utilization-dispatch --profile PROFILE --limit 100
cost-insight utilization-worker --profile PROFILE --limit 100
cost-insight utilization-maintain --dry-run
```

By default the machine planner reads the D-4 and D-3 Beijing `BillDay`
partitions. To override that set, repeat `--bill-day` (or its alias `--date`)
for each partition, with at most eight distinct dates per invocation. The planner
rejects dates before 1970-01-01 or after today in Beijing. Historical billing
partitions are not limited by the reporting year or usage-day horizon, but Tencent
can reject old partitions that are no longer available. Both products in every
requested partition must complete bounded scans before creating any tasks. A
product-filtered response can include other days in the same bill month; those
rows count toward the pagination budget but are not evidence that the other
partitions were requested. Machine planning
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

Tasks, daily contributions and completion use the shared V3 guard and DB clock.
Lease/deadline fencing rejects stale workers. Broker-confirmed publish precedes
marking dispatched; DB result/done commit precedes offset acknowledgement. Failed
fetches retry after a fixed interval until the fixed task deadline. Maintenance
never creates empty machine facts; before purging unfinished Pod work, it records
confirmed missing Pod-minutes for dates still retained.
Crash replays may repeat messages or an unfinished source window but not its committed
contribution. A bill extension adds only the uncovered tail when retained
windows form an exact contiguous prefix with the same snapshot. Conflicting
snapshots or other overlaps are rejected, not silently added as a second
version. New machine planning closes at the usage day's earliest
possible task expiry, so a shifted window cannot replace a purged checkpoint.
Do not clear done checkpoints or invent an ad-hoc repair.
Machine planning reports per-candidate snapshot/overlap validation failures in each
usage day's `errors` list (machine ID, region, reason), while continuing unrelated
machines and days. Conflicts roll back the affected candidate/day transaction.
A known billing discovery conflict persists across runs until a joint complete
rescan finds no conflict or the affected usage-day horizon closes. Omitting one
of its source partitions is not resolution. A scan must also retain its guard
generation through enqueue; rescan if another process changes conflict evidence.
At most 1,000 unresolved machines and eight BillDays per conflict are retained.
Overflow fails closed for all machine planning until `billing_hold_until` (UTC),
without blocking Pod dispatch or deleting existing results. Escalate repeated
overflow for source reconciliation; never clear the hold to force a replay.
The result separates scanned `bill_days` from the affected usage `days`.
If a long fee interval has a tail after the source reporting cutoff, `deferred`
reports the unplanned UTC range and BillDay partitions. Replay **all partitions
in that entry's `bill_days` together in one invocation**, with one `--bill-day`
per partition. Separate invocations may fail the existing-window overlap check.
Run after the tail's end plus the reporting allowance, before the relevant
usage-day horizon closes. For long tails, run periodic retries before earlier
usage days close. Nonempty `deferred` or `errors`, or nonzero `horizon_gaps`,
makes the CLI exit 2. Retry the relevant original partition set before the
usage-day planning deadline; the default D-4/D-3 scan does not revisit older
failed partitions. `BillDay` determines which data is read; the actual fee
interval determines which Beijing dates and windows are planned, including usage
in a different month. A complete billing scan
still does not prove an entire usage day's population is final: other partitions
may contain late records. V3 plans to reuse completed importer evidence.

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

Watch backlog/oldest due task, source errors, workload time coverage, retained
rows, table+index bytes, maintenance time
and pause reasons (`row_budget`, `byte_budget`, `bytes_unreliable`, `cleanup_backlog`).
A failed byte-statistics query invalidates previously healthy evidence if the
maintenance transaction can still update the guard. If the DB transaction cannot
write, the maintenance command fails instead of claiming it persisted a pause.
The workload API flags a selected date range as partial while the account has
unsettled Pod windows or confirmed unplanned evidence in the range; this is a
conservative warning across label filters. Coverage then describes settled windows
only. Machine status is a latest-observed-day signal, not proof of complete
coverage of that day or of the whole account.
Current warning1M rows/4GiB (reported in maintenance output's `warnings` list);
pause2M/8GiB or stale/unreliable maintenance/statistics after4h. All owned tables count toward the budget, including
machine facts. A paused producer does not stop maintenance. To disable only Pod
collection, register `pod_collection_enabled: false` with `--enable` so machines
continue. The registry stops new Pod admission at its DB cutoff; keep workers and
maintenance running for already registered windows and confirmed unplanned tails.
Completed workload results retain their normal query lifetime. Wait for the old
Pod workset to drain before re-enabling or changing its metric contract. Any
consumer still holding the old reader retries new-version tasks without querying
or writing results; a subsequent consumer with the new contract can settle them.
To stop all new collection, configure without `--enable` and keep maintenance running.
API disabled/missing-schema states must not break cost pages.
