# Tencent Pod Watcher: Initial Cost Estimate

Date: 2026-10-23. Currency: CNY unless explicitly marked USD.
Status: planning estimate, not a production invoice or an all-inclusive quote.
NO CLS is the approved implementation scope; the excluded CLS comparison below
is retained as background evidence, not a deployment option for this PR.
Discovery/price observations are inherited from the draft; this design-only
consolidation made no new online checks.

Related design: [TKE Pod lifecycle watcher](tencent-pod-watcher-design.md).

## 1. Decision: no CLS in this delivery

The recommended path is:

```text
Kubernetes Pods + core/v1 Events
  -> in-cluster watch-pods
  -> existing TiDB pod tables
  -> existing dashboard
```

This reads Kubernetes objects and Events, not container stdout/stderr or the
watcher Pod's own logs. Target workloads do not need an extra sidecar. The
watcher must persist the observations itself because Kubernetes Events are
short-lived and best-effort.

With no CLS export/log collection added, this lifecycle-data path has:

- **CLS ingestion, indexing, storage and query charges: 0.**
- No independent standard Kubernetes LIST/WATCH request tariff was found in
  the published TKE pricing. These are not CLS API requests. The existing TKE
  control plane still has base/capacity costs; a load-driven capacity upgrade
  would be a separate cost.
- Watcher compute, database storage/compute/I/O, and relevant network costs.

The existing GCP implementation has two distinct paths: `watch-pods` directly
uses Kubernetes; `sync-pods` reads Google Cloud Logging for supplementary history.
The latter is GCP-specific and is not proposed for TKE.

Skipping a logging API does **not** disable independent cluster log exporters.
If API auditing or stdout collection already exports to CLS/Cloud Logging,
additional watcher requests/logs can have indirect logging costs under that
policy. This estimate does not change any logging or audit configuration.

## 2. Read-only evidence

### Tencent service inventory

`cls DescribeTopics --region ap-beijing`, using the existing `ai-agent` profile,
returned `TotalCount=0` for log topics. This is only this profile/account/region's
visible log-topic inventory, not proof that every account/region has zero CLS
spend. No topic was created and no log-search scan was issued.

### Payload size sample

Three CI namespaces were sampled with bounded Kubernetes GETs. The current
normalizers produced these JSON sizes:

| Payload | Count | Mean | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Pod metadata snapshot fields | 124 | 1,366 B | 1,411 B | 2,174 B |
| Selected standard Event row | 322 | 850 B | 1,199 B | 1,284 B |

These are serialized selected fields, **not** billed DB disk, wire bytes, full
API objects, or a measurement of the daily workload. The sample does not include
all future CI templates, large annotations, or all proposed additional reasons.

### Compute price reference

A read-only `cvm InquiryPriceRunInstances` request for Beijing-8
`SA9.MEDIUM4` (2 vCPU / 4 GiB, hourly on-demand; a 50 GiB CLOUD_BSSD system disk
was supplied in the inquiry) returned:

| Returned field | Rate |
| --- | ---: |
| `InstancePrice.UnitPrice` | 0.31/hour |
| `InstancePrice.UnitPriceDiscount` | 0.07/hour |

The discounted result reflects the inquiry account/specification at this time,
not a guaranteed price for all TKE node pools or accounts. These returned
instance-price fields are used only as a compute allocation reference, not proof
that all disk, networking, tax, or related services are included in a final bill.
No VM was created.

With the proposed 0.5 vCPU / 1 GiB request, the reservation is approximately 25%
of this reference shape. At 730 hours/month:

```text
Allocated reference cost = hourly rate × 730 × 25%
Discounted reference: 0.07 × 730 × 25% = 12.78/month
List reference:       0.31 × 730 × 25% = 56.58/month
```

This is a **resource allocation/opportunity cost**, not a billable fractional VM.
If existing nodes fit the watcher without autoscaling, incremental VM spend may
be zero. If a whole extra reference node is needed, its returned instance-price
reference is 51.10/month discounted or 226.30/month list, plus any separately
charged disk/network. Do not add both fractional and whole-node costs.

The resource request is a starting configuration, not a throughput benchmark.

## 3. Initial volume model

Planning assumptions, deliberately above the observed mean payload sizes:

- 40 newly stored Event **versions** per newly created Pod, at 1 KiB each;
  this baseline excludes the proposed additional Tencent diagnostic reasons.
  Their incremental version volume is unmeasured and must be added after the
  canary before using this model as a firm budget. GCP watch/Logging reason
  allowlists are unchanged by this design.
- One lifecycle/metadata row at 6 KiB per Pod.
- 30-day retention and a steady-state month of 30 days.
- 2–3× payload allowance for table/index storage. This is a planning multiplier,
  not a measured TiDB disk amplification or a replica-billing rule.

```text
Monthly Event-version rows = Pods/day × 30 × 40
Monthly payload GiB = Pods/day × 30 × (40 × 1 KiB + 6 KiB) / 2^30
Steady retained DB footprint ≈ payload GiB × 2–3
```

| New Pods/day (assumed) | New Event-version rows/month | Payload/month | DB footprint at 30-day retention |
| ---: | ---: | ---: | ---: |
| 1,000 | 1.2 million | 1.32 GiB | 2.6–4.0 GiB |
| 10,000 | 12 million | 13.16 GiB | 26–40 GiB |
| 100,000 | 120 million | 131.61 GiB | 263–395 GiB |

The same Kubernetes Event object can have multiple resourceVersions, which the
current watcher stores separately. Retry/pressure storms can increase this
ratio greatly. These counts must not be interpreted as unique scheduling or
image-pull attempts. Longer retention scales the steady retained footprint;
backups and provider-specific replica/compute charges are additional.

## 4. Database and network: the main uncertainties

### TiDB is not free just because its cluster already exists

The database still has additional INSERT/UPDATE, reads for Event rollups, Pod
metadata and build attribution, index maintenance, and dashboard queries.
Existing provisioned headroom may absorb compute without an immediate resize;
serverless/RU or usage-based plans may charge immediately. Storage can also be
usage-based even when compute is provisioned.

The TiDB deployment, billable storage/RU rates, spare capacity and exact network
location were not verified for this estimate. Therefore **do not quote a fixed
all-inclusive monthly total** from the table above.

```text
Incremental DB cost = billed storage × applicable storage rate
                    + extra compute/RUs/I/O/backups under the actual DB plan
```

### A stored row is not the same as one network transmission

Current `pod_watcher.py` relists and reprocesses collections after normal
300-second watch turnover, not just after failures. At eight namespaces and two
streams per namespace, this is roughly 280,000 LIST/WATCH connection requests
per 730-hour month before pagination/retries. No separate per-request tariff was
found in the recorded pricing evidence; the calls still consume API, database
and network capacity. Normal-timeout relist optimization is outside this PR,
so this estimate does not credit unimplemented traffic/DB savings.

Relisting an unchanged Event can upsert it again; idempotent identity prevents a
new row, not another database operation. Each live Event can also read existing
Pod metadata and rebuild/update its lifecycle. Thus wire traffic and DB operations
can be many times the unique-row payload model. The 13 GiB example must not be
used as an estimate of all cross-cloud traffic.

For a public, traffic-billed Tencent mainland egress route, the official CVM/EIP
reference is **0.80/GB** (1024-based units). Thus an **illustrative**, not measured,
20–100 GiB/month of Tencent outbound traffic costs 16–80/month. Full wire bytes,
including protocol overhead/retransmissions, are the billable basis.

The actual route may use a fixed-bandwidth/shared-bandwidth plan, private
connectivity, NAT, VPN or another service. Apply that route's tariff instead.
Additional NAT gateway processing/fixed fees or new private connectivity can
outweigh the watcher itself and are not included here.

If the DB is hosted outside Tencent, also account for the **DB-side provider's
outbound responses**. SQL attribution/metadata reads may be important; Tencent
inbound pricing does not make the opposite cloud's outbound free. Do not assume
the DB is in GCP, or assign a GCP region/egress rate without verifying it.

## 5. Practical preliminary budget

For an illustrative 10,000 new Pods/day, 30-day retention, a single watcher and
no new network gateway/connection:

| Item | Initial estimate |
| --- | ---: |
| Lifecycle data through CLS | 0/month; not enabled/used by this design |
| Standard Kubernetes API request charges | No separate per-request tariff found in recorded evidence |
| Watcher compute, allocated reference | 13–57/month |
| Tencent outbound, **assuming** 20–100 GiB/month and traffic billing | 16–80/month |
| Known-rate subtotal, allocated compute basis | **about 29–137/month** |
| TiDB | 26–40 GiB planned retained footprint + actual compute/I/O/backups; unpriced |
| DB-provider outbound, NAT/VPN/new connectivity, separate disk | Not included; route/plan unverified |

If existing nodes have spare capacity, remove the 13–57 allocation when estimating
**incremental cash spend**: the corresponding known-rate subtotal is 16–80/month,
plus the unpriced items. If an extra node is required, substitute its full
incremental price instead. This is not a zero-cost claim or a complete quote.

Recommendation: start without CLS and without new dedicated network
infrastructure. During the first approved 24-hour canary, measure actual daily
Pod/version counts, new DB bytes, SQL operations/RUs, live/relist traffic, and
watcher utilization. Extrapolate a steady month and verify the account's actual
DB/network rates before approving a firm monthly budget.

Cost controls requiring no second telemetry stack:

- collect only target namespaces and lifecycle/diagnosis reasons, not test logs;
- confirm existing retention/cleanup policy for raw Events and rollups; any policy
  change needs separate ops review, not an assumed 30-day deployed setting;
- avoid dumping every raw Pod/Event into watcher stdout and another log service;
- measure repeated relist/upsert costs; a later, separately reviewed optimization
  may resume normal WATCH turnover from processed resourceVersion while preserving
  relist-on-expiry/health behavior; no such optimization is included here;
- keep build attribution scoped/cached and avoid sending redundant full snapshots
  over a cross-cloud DB connection.

## 6. Excluded alternative: persist TKE Events in CLS (reference only)

TKE's Event persistence is explicitly enabled and sends Events through an event
collector to a configured CLS topic; the documented workflow enables topic
indexing by default. This is a separate path, not a prerequisite for direct
Kubernetes watching. It can provide a retained Event history, but does not by
itself capture every deleted Pod's creation time/labels or make Events lossless.

### Mainland standard-log, functional-billing public rates

The official product pricing and billing overview were updated 2026-10-16.
Beijing/Shanghai/Guangzhou/Nanjing/Chengdu/Chongqing public rates include:

| CLS item | Public rate | Measurement |
| --- | ---: | --- |
| Log write traffic | 0.18/GB | Compressed bytes |
| Standard index traffic | 0.35/GB | Uncompressed indexed fields/values |
| Standard log storage | 0.0115/GB/day | Compressed retained volume |
| Standard index storage | 0.0115/GB/day | Uncompressed retained index volume |
| Service requests | 0.15/million | Batched request count, not Event count |
| Topic partitions | 0.04/partition/day | Number of partitions |
| Intranet consumption/delivery | 0.18/GB | Actual selected compressed/uncompressed volume |
| Internet consumption/download | 0.80/GB | Actual selected compressed/uncompressed volume |

Normal query/SQL analysis has no separately published scan-GB charge; it is not
equivalent to log consumption/download. Official documents differ on whether
all query API requests fall under service-request charging, so conservatively
reserve the request fee rather than claiming all query calls are free. Ordinary
query results should not be charged again as downloaded consumption GB.

Functional billing has **no free storage period**. A distinct raw-log billing
mode charges 0.35/uncompressed GB for standard logs and includes 30-day storage,
but is currently whitelist-only. Do not combine both modes or assume that the
account is eligible for the latter.

### CLS example, not an estimate of this cluster's measured daily volume

Assume raw log input `I` GB/day, 20% compressed size, indexed volume equal to raw
input, standard storage, 30-day retention, one partition, and a full 30-day
steady-state billing month:

```text
Write traffic       = 30 × I × 20% × 0.18
Index traffic       = 30 × I × 0.35
Log storage         = 30 × (I × 20% × 30) × 0.0115
Index storage       = 30 × (I × 30) × 0.0115
One topic partition = 30 × 0.04
Subtotal            = 24 × I + 1.20 CNY/month
```

| Assumed original CLS input | Estimated monthly CLS subtotal |
| ---: | ---: |
| 1 GB/day | 25.20 |
| 10 GB/day | 241.20 |
| 100 GB/day | 2,401.20 |

Add service requests, any consumption/download, collector compute, relevant
network transport and any optional processing. Compression/index proportions
are assumptions, not a promised ratio. The initial empty-topic month has lower
average retained volume than steady state. More partitions increase the fixed
portion; additional application/audit logs can dominate Event-only volume.

This is **not** a one-to-one comparison against the 13 GiB normalized TiDB row
example: the event collector's raw JSON, namespaces/reasons and export policy
can differ. Also, CLS history does not eliminate the watcher/database costs if
the dashboard still needs Pod metadata and relational lifecycle metrics.
At small Event-only volumes, CLS itself may be inexpensive; use the simpler
direct path initially because it satisfies the requirement, not because CLS
would necessarily be the largest cost.

## 7. GCP price clarification

Ordinary Cloud Logging queries are not separately charged per read in the
current published pricing. Standard log storage/ingestion is 0.50 USD/GiB after
the first 50 GiB/project/month, including up to 30 days' log-bucket retention;
ordinary querying/analysis has no additional charge. Extended retention and
other export/destination/network products may have their own costs.

Therefore it is more accurate to say the GCP supplementary path depends on a
paid logging/storage service, not that every `entries:list` read is a separate
Cloud Logging read charge. The Kubernetes direct-watch alternative is available
on both clouds; it does not automatically disable GKE's independent log export.

## 8. Price sources

Recorded read-only price check date: 2026-10-23; not revalidated in this phase.
Recheck at separately approved rollout; account contracts and actual routes take
precedence over these references.

- [TKE standard cluster billing](https://cloud.tencent.com/document/product/457/68803)
  — cluster management and cloud-resource fees; no independent LIST/WATCH tariff listed.
- [TKE Event logs](https://cloud.tencent.com/document/product/457/32091)
  — explicit Event persistence into CLS and default indexing.
- [CLS product pricing](https://cloud.tencent.com/document/product/614/45803)
  and [billing overview](https://cloud.tencent.com/document/product/614/45802)
  — 2026-10-16 rates, byte bases, billing-mode and retention restrictions.
- [CLS query/analysis overview](https://cloud.tencent.com/document/product/614/86832)
  — query request charging and download distinction.
- [Tencent public-network pricing](https://cloud.tencent.com/document/product/213/51767)
  — page updated 2026-08-28; mainland traffic reference 0.80/GB.
- [Tencent CVM price inquiry API](https://cloud.tencent.com/document/api/213/15726)
  — live read-only `InquiryPriceRunInstances` result described above; not an order.
- [Google Cloud Observability pricing](https://cloud.google.com/stackdriver/pricing)
  — reached via the official Cloud Logging pricing page.
