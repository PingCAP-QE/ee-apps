# Tencent utilization: opt-in operations

Implementation baseline: [collection design](resource-utilization-design.md) and
[retention design](resource-utilization-retention-design.md). This feature is
**disabled by default**. No production migration, cloud canary, deployment, or
production data write was performed as part of implementation.

## What ships

- Existing Tencent CPU/memory monitoring is read in bounded, transient 30-minute
  windows. Only daily machine, repo, stable Job/specification, and account-class
  summaries are published; minute samples are not archived. Fully covered minute
  peaks and observed-subset peaks have separate values, timestamps and coverage;
  an observed subset is never promoted to a full-population peak.
- Daily refresh uses a common 10-minute reporting lag, recomputes current/previous
  Beijing day, and replaces the whole date atomically. Physical machine totals
  and Pod consumer scopes remain separate. Node/CVM aliases are deduplicated.
- Usage/physical-capacity, usage/Pod-requests, and requests/Node-allocatable ratios
  have distinct pinned contracts. Percentages are capacity/time weighted;
  peaks are synchronous minute means. Missing metrics are not zero. Unknown
  attribution and requests remain visible, including known absolute Pod usage.
- CI Dashboard's existing Pod watcher exports only whitelisted effective evidence
  and adds paginated Node LIST every 30 minutes. Healthy WATCH heartbeats roll
  confirmed-live UIDs into separately expiring weekly slices. Identical Node/CVM
  inventory samples bridge a week boundary only within35/45 minutes respectively;
  changed/stale samples leave unknown poll gaps rather than invent old capacity.
  Clean WATCH timeouts resume the latest event/BOOKMARK cursor without relisting
  or creating another interval. Real gaps/auth/version errors reset continuity.
  Terminal snapshots not observed live create no useless zero-duration history;
  an observed live UID closes once. It reuses the existing cluster connection.
  Cost Insight has no Kubernetes client, token, or Node RBAC.
- Daily facts/evidence expire at Beijing Monday +14 days, independently of
  enrichment. Only account-class weekly results survive. Separate maintenance
  finalizes missing/incomplete weeks and deletes expired details in committed
  batches (1,000 rows, at most 50,000 rows or 5 minutes per invocation).
- Writers share one fenced guard row; disabled/unhealthy/expired/closed writes
  fail closed. Warning thresholds are 1M rows/4 GiB; pauses at 2M rows/8 GiB
  count both temporary tables and indexes. These are admission/monitoring
  thresholds, not hard physical disk quotas. Row counters, expiry selection and
  deletion share the target account scope; byte monitoring conservatively covers
  both full tables and indexes. Foreign-account rows are never deleted or allowed
  to block target-account expiry selection. Recovery requires healthy maintenance
  and backlog cleanup; a hard pause resumes below warning thresholds.

No disk telemetry, personal utilization, full Pod objects, raw-response archive,
soft-delete copy, object-storage export, or precise Job cost projection is added.
Existing billing and personal chargeback remain unchanged.

## Source contract and P0 checks

`config/tencent-utilization.example.json` is deliberately **not ready to enable**:
conversion scales/horizons are zero and evidence fields are empty. Copy it to an
operator-managed file, never put credentials in that JSON. Regions/namespaces
are explicit scope, not member/region discovery. Match namespaces with the
existing Tencent watcher; the template does not claim unselected workloads are
covered by consumer summaries. Physical totals cover configured regions/cluster,
not undiscovered future regions or clusters.

The known account has ordinary CVMs and native TKE nodes. `tke_cvm` consumer
contracts always exist. By default CVM-backed TKE machines remain in the ordinary
CVM physical scope (queried by historical node IDs as well as inventory), and their
Pods remain in the separate `tke_cvm` consumer scope. `include_tke_cvm=true`
opts into a separate physical TKE-CVM class/calendar and deduplicates its CVM alias;
it is not required merely to collect Pods on CVM-backed nodes. Unrecognised node
IDs retain Pod usage in `tke_unknown`; unmapped physical sources are flagged, never
silently counted as known machines. Consumer-only classes never create weekly
machine expectations. New physical classes or changes to source/scope contracts fail closed;
configure does not silently rewrite an existing registry's start/contracts.
An effective-week contract evolution requires a separately reviewed operation.

Read-only capability sample (opens no database):

```bash
cd cost-insight
cost-insight utilization-probe --profile config/tencent-utilization.example.json
# Optional: inspect one historical 20-minute window, not a whole-day proof.
cost-insight utilization-probe --profile config/tencent-utilization.example.json --date YYYY-MM-DD
```

Credentials: explicit `TENCENTCLOUD_SECRET_ID`, `TENCENTCLOUD_SECRET_KEY`, optional
`TENCENTCLOUD_TOKEN`; required read actions are STS GetCallerIdentity, CVM
DescribeInstances, and Monitor DescribeBaseMetrics/GetMonitorData/
DescribeStatisticData. The root account identity must be `100050658403`.
SDK response DEBUG logging is suppressed. Never save full probe responses.

The sample reports units/periods/counts, **not** verified replay/conversion or
cost. Verify and record all of the following before activation:

1. Real full-day repo/stable-Job linkage, unknown coverage, Node provider-ID
   coverage, requests/limits and capacity effective intervals; restart/relist
   gaps must not be backfilled with current labels. Node sampling is metadata,
   not a new high-frequency usage sampler.
2. Source units and conversion factors, including Tencent's `MBytes` semantics;
   do not assume decimal vs binary merely from a metric's name. Verify timestamp
   alignment and 60-second `avg` semantics. Metadata units/statistics are checked
   again at collection time; no coarser fallback is permitted.
3. Actual full-day queryable horizons for **every** metric and Pod/Node selector,
   including disappeared nodes. Node queries use verified cluster-only/no
   GroupBys; Pod queries batch explicit node IDs. Test representative windows
   across the proposed horizon, not only one successful probe. Set the minimum
   demonstrated `verified_horizon_seconds`, a safety margin, conversion factors,
   `horizon_verified_at`, `evidence_reference`, and
   `timestamp_alignment_verified=true` in the operator profile.
4. TiDB pessimistic/READ COMMITTED behavior, shared guard contention, selected
   indexes/query plans and nullable JSON handling. Offline SQLite/dialect tests
   are not evidence of real TiDB execution.
5. Peak-day cardinality, bytes/API calls/retries, runtime, database RU, data+index
   size, transaction size/time and cleanup throughput under backlog. Initial
   publication ceilings (10,000 rows, 16 MiB input, 10-second transaction with
   rollback) and 2 GiB job memory limit are **unmeasured canary ceilings**, not
   performance claims. Run the isolated six-week lifecycle/failure tests and a
   pressure canary before enabling ingestion. Include hot-guard JSON write
   amplification (the immutable contract catalogue shares the rewritten guard row,
   bounded to 64 KiB), lock contention across namespace workers, actual event-burst
   batch size/flush rate, heartbeat updates and their total RU/bytes. Batching
   coalesces available HTTP-read bursts and flushes before the next blocking read,
   deletion, gap or stop. It caps pending input at1,000 records and checks a1-second
   event-processing deadline; it does **not** promise one transaction per second
   or a1-second bound on synchronous DB I/O. Sparse events may still be individual
   transactions. No durable event backlog, artificial delay, timer or indefinite
   retry is added.

The historical ¥100/month figure remains a calibration budget, not a guaranteed
price or cap. See [validation/cost uncertainties](tencent-utilization-validation-and-cost-estimate.md).

## Activation sequence (operator review required)

1. Review/apply `sql/031_create_utilization.sql` against the intended TiDB database.
   Cost Insight owns these tables and the existing `cost_job_state` guard. Give
   CI metadata only required observation SELECT/INSERT/UPDATE and guard
   SELECT/UPDATE permissions; it does not need daily/weekly writes or DDL.
2. Initialize a **disabled** immutable scope/calendar using a completed profile:

   ```bash
   cost-insight utilization-configure --profile /etc/utilization/profile.json --start-week YYYY-MM-DD
   cost-insight utilization-maintain --dry-run
   cost-insight utilization-maintain
   ```

   The start date must be a Beijing Monday. Dry runs issue SELECT only and never
   initialize the guard, refresh a watermark, mark facts stale, or clean anything.
3. Review `ci-dashboard/k8s/utilization/nodes-readonly.yaml`, replace its explicit
   existing service-account binding, and grant only nodes get/list in the Tencent
   cluster. Do not bind this to Cost Insight or create another cluster secret.
4. Render **all three suspended** CronJobs together for review:

   ```bash
   python scripts/render-utilization-cronjobs.py \
     --namespace <existing-jobs-namespace> --image <reviewed-cost-jobs-image> \
     --db-secret <existing-database-secret> \
     --credential-secret <readonly-tencent-secret> \
     --profile-configmap <operator-profile-configmap>
   ```

   The profile ConfigMap key is `profile.json`. Maintenance gets only database
   credentials; it needs neither Tencent credentials nor a profile mount.
   No Cost Insight Pod mounts a Kubernetes service-account token. Rendering has
   no Kubernetes side effects. Apply/unsuspend only after the separate canary
   approval; activate maintenance first and keep it independent of ingestion.
5. Explicitly opt in after source/DB/lifecycle/throughput evidence is accepted:

   ```bash
   cost-insight utilization-configure --profile /etc/utilization/profile.json \
     --start-week YYYY-MM-DD --enable --confirm-source-horizons
   cost-insight utilization-maintain
   ```

   Set `CI_DASHBOARD_UTILIZATION_ENABLED=true` on the existing Tencent watcher
   and dashboard API, then activate inventory (every30m) and daily refresh
   (00:30/06:30/12:30/18:30 Asia/Shanghai). Maintenance runs every15m.
   The API/UI provide `/utilization` and `/api/v1/utilization/{status,daily,weekly}`;
   disabled/missing-schema states are informative and existing cost pages work.

## Repair, stop, recovery

- A routine successful date is last recut on D+1 18:30. Later attribution is not
  silently applied. Use `sync-tencent-utilization --profile ... --date YYYY-MM-DD
  --repair [--dry-run]` for an explicitly approved, still-open retained day.
  Source/evidence/retention horizons must all permit replay. Missing/stale days
  are visible in status; older failed days require this bounded explicit retry,
  not an unbounded backfill daemon.
- Explicit verified-empty repair is distinct from missing data: add
  `--empty-proof /path/to/proof.json` to `--date ... --repair`. The bounded proof
  must assert `confirmed_empty=true`, the exact vendor/account/date/cutoff,
  `resource_classes`, pinned `source_scope_hash`, `verified_at` and a short
  `evidence_reference`. It is an operator-reviewed assertion of **no resources**,
  never inferred from zero points or zero usage. Contradicting monitoring/resource
  evidence rejects it. It creates only account-class zero sufficient statistics
  with NULL utilization, coverage1 and a proof digest; no fake resource/Job rows.
  Seven proven-empty days may be complete; seven absent days remain missing.
- Coverage is numeric when entity-time is known: the minimum of known-capacity
  coverage and matched-denominator entity-time coverage. Unknown and known-zero
  denominator seconds are separate. Zero Pod requests give NULL efficiency, not
  unknown metadata. A week meets the95% criterion only without hard flags such as
  unknown physical capacity, stale/partial days, missing dates or unmapped physical
  sources; a small `missing_points` flag alone does not override a95%+ ratio.
- Source/aggregation failure preserves the previous daily values and marks them
  stale; a partially read date never replaces them. An older cutoff or decreased
  physical evidence cannot overwrite a newer/trusted publication. The same
  summed evidence checks cover Job/repo classes (including usage-observed seconds
  when requests are unknown); spec/repo/Job regrouping and unknown-to-known
  enrichment do not count as data loss. Completed
  whole-day empty proofs remain valid when a later successful read has no
  contradicting class rows; partial empty proofs are never extended automatically.
  Unverified/exhausted source
  horizons also preserve the last trusted result; missing data never becomes
  utilization zero. With no trusted result, account history ultimately closes
  as `final_missing` or `final_incomplete` and details are still purged.
- Stop producers without stopping maintenance. For a permanent collection stop,
  repeat configure without `--enable` and add `--stop-week YYYY-MM-DD` (inclusive
  Monday). This bounds future expected-week enumeration instead of manufacturing
  missing weeks forever. `None`/omitted stop preserves an existing stop.
- DB outage, expired maintenance/stats, unknown counts, overdue cleanup, or hard
  budgets stop detail admission. Resume maintenance first; it reconciles counters,
  closes overdue weeks and drains expired details before writers can resume.
  Updates never renew expiry. Weekly summaries remain; no manual resurrection of
  closed detail weeks or raw-response recovery copy is provided.
