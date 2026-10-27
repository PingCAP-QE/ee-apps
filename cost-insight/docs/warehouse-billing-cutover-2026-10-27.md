# Warehouse billing read cutover — 2026-10-27

## Scope and rollout boundary

This change prepares keyless SA reads and validates source compatibility. It does
**not** deploy a new image, change live CronJobs/cost_sources, run an importer,
backfill costs, or republish attribution/resource-serving facts.

- Target SA: `hxprog-e5d756e67e8641f386ab0e9@warehouse-f80fbbe8e8.iam.gserviceaccount.com`
- Query project: `warehouse-f80fbbe8e8`
- Location: `us-west1`, not `US`
- Approved table-access expiry: `2027-04-25T06:52:47Z`
- Staged profile: [`../config/warehouse-billing.env.example`](../config/warehouse-billing.env.example)

The new billing-only client supports project, location and short-lived SA
impersonation. All billing exporters (including resource reads and reconciliation)
use it. Unset options retain ADC defaults. GCS cache clients are unchanged. A
production caller must separately have permission to impersonate the target SA;
local impersonation success does not establish the runtime KSA's permissions.

## GCP: compatible raw source verified

The default source now matches the live warehouse export:

```
warehouse-f80fbbe8e8.raw_gcp_billing.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6
```

Using the staged profile, `SESSION_USER()` confirmed the target SA and the client
reported the expected query project/location. The application's existing
`build_gcp_billing_summary_query` executed successfully for
`pingcap-testing-account`, export partition and earliest usage date `2026-10-26`.
Only a BigQuery `SELECT` ran; no database was required or connected.

- Job: `fce97280-96fb-4eb7-b07e-55629ff8d839`
- Dry-run estimate / actual bytes processed: `2,539,895,616`
- Maximum bytes billed: 3 GiB
- Three normalized sample rows returned; existing cost and GKE dimension columns
  compiled without schema changes.

A prior 1 GiB safety cap stopped execution after dry-run; it did not run a data
query. The successful bounded query used the explicit 3 GiB cap above.

## AWS: compatibility gate

### Accepted cost policy (2026-10-27)

The user confirmed that list cost is the primary cost-analysis measure on AWS
and GCP. For this AWS source cutover, discount-driven effective/net-cost
mismatches are informational, not blockers. Preserve the existing AWS
`list_cost` definition: Usage + SavingsPlanCoveredUsage unblended cost, with
EDP/PrivateRateDiscount rows contributing zero to list cost. Do not substitute
`pricing_public_on_demand_cost`, change GCP's list-cost definition, or erase
raw discount/effective/net amounts.

This changes **cross-source cutover acceptance**, not same-source
raw-to-summary-to-attribution integrity checks or importer allocation guards.
List-cost conservation, attribution dimensions, source scope and freshness
remain required.

The account-level freshness comparison source is:

```
warehouse-f80fbbe8e8.mart_cloud_cost.aws_member_daily_cost
```

Its grain is account/day/service/region/currency/charge_type. It has unblended and
net-unblended amounts plus source line counts, but lacks tenant/usedby, resource
IDs, SKU-level identity, Kubernetes workload dimensions and parent/child split
lineage. Therefore account-total freshness alone does **not** justify replacing:

- legacy CUR account/tenant attribution;
- `946646677266` split-cost resource/workload allocation;
- `380838443567`'s `usedby = 'prod-us-west-2-f04'` boundary.

Keep these registered importer sources unchanged until a stable, accessible
full-detail source can preserve their semantics. Temporary `_stage` tables are
not a stable production source contract. Amount comparisons must distinguish
`Usage` + `SavingsPlanCoveredUsage` from all-charge totals, account-wide data
from the F04 subset, and future fee/discount dates from actual usage freshness.

### Full-detail staging sources and required adapter changes

Two approved `warehouse-f80fbbe8e8.raw_aws_billing` tables are readable:

- `_stage_202610_228e2ed1a4ec4e4c_ecfd2884ae15`
- `_stage_202610_bff5de906b2c03d7_173c5a84bb3e`

Both have 125 columns, monthly billing-period partitioning, account/product
clustering, nested `resource_tags.key_value`, resource IDs and all 13 split
fields. The six-account/two-day control sums and row counts match between these
two snapshots. **Do not UNION them as incremental data.** Dataset table-list
access was denied; no stable production CUR object/lifecycle was established.

The raw data can retain attribution dimensions, but a table-name-only switch
will not work: derive the billing month from `bill_billing_period_start_date`,
extract nested `user_usedby`, `user_tenant`, `user_project`, `user_cluster` tags
for legacy aliases, and map nested tags to split/F04 flattened column names.
A minimal compatibility relation now implements these aliases. All seven
query builders accept explicit `warehouse_raw=True` while their default SQL
fingerprints remain unchanged. This mode is available for read-only validation,
not enabled in importer fetch/dispatch or production source profiles. The
stable source contract must be settled before wiring that production route.

### Read-only sample results: 2026-10-11 and 2026-10-12

All amounts below are USD. `L` means Usage + SavingsPlanCoveredUsage unblended
cost; `E` means all-row unblended cost. Sample unblended values were non-NULL,
so E also equals the current adapter's effective-cost fallback calculation.
The initial sample round used dry-run estimates and an explicit maximum-billing
cap of at most 5 GiB; its maximum actual scan was 4,979,672,221 bytes. The
subsequent, single bounded AmazonEKS completion query used the separately stated
15 GiB cap documented below.

**Four ordinary legacy accounts:** `131464424160`, `296171618728`,
`676546228356`, `986330900858`. Each account/day's L and E matched the old
October default-source table to less than **$0.000004**. This is an amount check,
not proof that the complete normalized importer output is identical.

**Split account `946646677266`:**

| Usage date | Old L | New L | Old E | New E | New E minus old E |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-10-11 | 478.813723 | 478.813723 | 196.631901 | 129.384646 | -67.24725468 |
| 2026-10-12 | 720.245841 | 720.245841 | 356.603973 | 215.800534 | -140.80343861 |

The new raw source includes EDP and PrivateRateDiscount rows that the actual old
split source omits. Those rows explain the effective-cost deltas exactly.
Under the accepted list-cost-first policy above, these deltas are informational
and no longer block the source cutover. The split list-cost sample passes;
full normalized-output and remaining source-scope checks are still required.

For Usage/SP list and split-cost amounts, **2,164 nonzero joint dimension groups**
matched with no delta over $0.000001 across 11 labels: usedby, tenant, cluster,
shared_pool, icost owner/project/service/service_exec_id, and EKS
namespace/workload name/workload type. Parent counts matched (244/514 per day),
and both days had zero unmatched parents and zero list/effective allocation
overshoots above $0.01 under the existing guardrail semantics. This establishes
raw-dimension/parent-child compatibility, not final per-fact importer equality.

**F04 account `380838443567`:** exact account + USD +
`usedby = 'prod-us-west-2-f04'` + Usage/SP filters were used on both sources.
The new nested raw source has real matching rows. Of 39 product codes, 38 were
fully queried; their 16 service/tenant/project/cluster groups matched the old
source in row count and dimensions, with amount differences below $0.000001.

| Usage date | Old complete F04 amount | New verified 38-product subtotal |
| --- | ---: | ---: |
| 2026-10-11 | 120.8641539597 | 118.4641539680 |
| 2026-10-12 | 123.3278042771 | 120.9278042769 |

**The table above records the initial, deliberately incomplete comparison.**
That round skipped AmazonEKS after a 12,983,662,977-byte dry-run estimate; it was
not treated as zero or filled from the old result.

On the follow-up verification, the EKS-only query dropped unused effective-cost
columns, estimated/scanned **12,530,617,705 bytes**, and executed once with an
explicit **15 GiB** maximum-billing cap. It returned **24 rows/$2.40 per day**,
with tenant `admin` and null project/cluster, exactly matching the old scope.
Job: `a3b7e0f4-c593-4ce0-84c8-21dc1bd3538a` (warehouse/us-west1).

The completed **39-product** F04 comparison now has **18 identical joint
service/tenant/project/cluster groups**, no missing/additional groups and no
row-count mismatches. Maximum group list-cost delta: **$0.00000000849996**.

| Usage date | Old complete rows | New complete rows | New complete list cost |
| --- | ---: | ---: | ---: |
| 2026-10-11 | 7,688 | 7,688 | 120.8641539680 |
| 2026-10-12 | 7,935 | 7,935 | 123.3278042769 |

The complete F04 raw-scope/list-cost gate passes. Account-wide control sums
still are not substitutes for this exact F04 tenant-boundary comparison.

### AWS gate decision and audit queries

**Not approved for production-source replacement or backfill yet.** Next gates:

1. Source owner supplies/confirms a stable full-detail CUR object and retention
   contract, with the required read permissions.
2. Finish production source-layout opt-in after the stable object contract is
   confirmed. The compatibility builders and legacy/split normalized checks
   below are implemented and verified; preserve F04/split source boundaries,
   complete the remaining F04 SKU-grain check and retain scoped replacement.
3. Verify the cluster runtime identity and use the correct direct-WI versus
   impersonation configuration (see runtime findings below).

The split discount policy is resolved: use list cost for cross-source acceptance;
retain effective/net differences in the diagnostic report without requiring
cross-source equality. The F04 AmazonEKS comparison is also complete and passes.

Representative BigQuery jobs (new jobs in warehouse/us-west1; old jobs in
pingcap-testing-account/us-west2 or US as noted):

| Check | New job | Old job |
| --- | --- | --- |
| Six-account/day raw totals | `cfc72d66-595a-4cb3-90de-d2bda4fb57cd` | `d81d922c-ad79-4645-a93a-4fd9fffad66b` (us-west2) |
| First staging control | `2be88394-68b9-4294-be09-40893a0951db` | same default-source control |
| Split charge-type deltas | `44e3bc80-3a5c-49ba-8788-600551c5fc02` | `961813ca-8ec2-468f-b78f-cb305806225a` (US) |
| Split joint dimensions | `d4aeee36-9096-47da-89d9-3b04735f2db0` | `c38dd30e-0d08-486c-a97b-6ba6c0b8e017` (US) |
| Split parent/guardrail check | `04787961-f2d8-48eb-a82a-547e34ab0545` | `75fcecb5-0e69-4601-9f75-e8ee2d5a5105` (US) |
| F04 full old filter / dimensions | see subset jobs below | `c74ea6c9-b5a9-41c3-b3c8-193ee72d01d7`, `46ccc773-caaa-48db-9d1e-bc0851a245e4` (us-west2) |

F04 new subset jobs: `be0c4136-e337-4c47-a681-cadf9732cad1`,
`7c9bd370-8b21-4c94-8beb-5b8fa9cda4e0`,
`30ba3b20-501f-4edc-b39c-833f832ab55b`,
`cc2093c1-06ef-432e-92c7-8df847b2872d`,
`58ac7438-e9a8-440c-b4a3-c78ba639cefa`,
`18cdaa9f-c536-4179-bd1b-efc0bef22ac1`. Together they cover the 38 verified
products; the follow-up job `a3b7e0f4-c593-4ce0-84c8-21dc1bd3538a` completes
AmazonEKS without substituting old-source values.

## Normalized-output and deduplication verification

Using `warehouse_raw=True`, all seven builders (legacy summary/resources, F04
summary, split summary/resources/guardrail/parent-residual) passed real BigQuery
dry-run compilation. This verifies the nested-tag aliases against the actual
125-column raw schema, not merely Python string tests. Query costs were checked
before execution.

### Legacy normalized summary: account 296171618728

Executed the application's complete summary query and normalizer for
2026-10-11/12 and compared it with a read-only production summary snapshot:

- 3,482 old and new normalized rows; all source-row hashes match, including
  all 3,222 rows with nonzero list cost.
- No duplicate normalized hashes; maximum common-row list-cost delta is
  $0.0000000078.
- Job `69303911-6875-4e9a-b915-0d65fcb70a41`, warehouse/us-west1,
  563,329,137 bytes processed, 5 GiB cap.

### Split normalized summary: account 946646677266

Executed the complete old and new summary builders, not just raw group sums:

- Old raw normalized output exactly matches the existing production snapshot's
  11,137 identities (list amounts within $0.000000001).
- New raw produces 11,512 rows, with no duplicate normalized hashes.
- Daily list-cost totals are conserved to within $0.000000003:
  $478.813723147 → $478.813723144 and $720.245841685 → $720.245841685.
- **43 old and 45 new nonzero-list identities differ.** After canonical tag
  normalization, all 43 old identities match new rows when only `repo/project`
  are excluded: those fields were null in the old source and are now populated
  from genuine provider `user_project` tags. Two additional SecretsManager
  project groups each move $0.051612902 per day out of an existing group; this
  changes project distribution, not the account/day total.
- New EDP/PrivateRateDiscount rows contribute additional zero-list facts and
  diagnostic effective/net amounts. They are not a list-cost mismatch.

An upsert-only union would retain stale identities, overstating list cost by
$0.175992985 / $0.171841825 on the two days. **Do not implement this migration as
blind upsert.** The existing complete split-sync path already spools and
atomically replaces the observed usage-date scope; preserve that behavior and
do not use limited/incomplete source reads for replacement.

The actual old/new normalized rows were replayed through the existing
`replace_summary_usage_dates` helper twice in **local SQLite `:memory:` only**:
11,512 new identities exactly remained after each replay, counts/totals were
unchanged on repetition, and a sibling account and outside-scope date were
preserved. No production database write or importer ran.

- New normalized job: `0105a6bb-265d-4d01-a593-7e2efd59ddbc`, warehouse/us-west1,
  2,625,984,948 bytes, 5 GiB cap.
- Old normalized control: `6d48f477-a6e1-4aa5-a092-1681f52ab522`,
  pingcap-testing-account/US, 2,715,526,240 bytes, 5 GiB cap.

### Remaining normalized-output boundary

F04's exact raw scope, all 39 products and 18 service/tenant/project/cluster
groups have passed the completed check above. The full SKU-grain normalized
F04 builder also compiles for the real F04 account, but estimates
23,832,299,692 bytes and was **not executed** under the regular 5 GiB follow-up
cap. Therefore the complete normalized F04 hash comparison is not claimed.
The one-time higher EKS budget did not authorize unrestricted scans for all
remaining queries. Either use a more selective stable detail source or hold
F04 separately until this final check is completed.

## Stable-source lifetime findings

**Two separate clocks must not be conflated:** the approved SA/table-access
authorization lasts until **2027-04-25T06:52:47Z**. The table resources themselves
have independent TTLs, read from the BigQuery Table API's `expirationTime`
property. The table TTLs below were independently rechecked at
**2026-10-27 09:16 UTC** and were unchanged. All times below are UTC:

| Accessible raw artifact | Expires |
| --- | --- |
| AWS `_stage_202610_228e2ed1a4ec4e4c_ecfd2884ae15` | 2026-10-28 05:42:55 |
| AWS `_stage_202610_bff5de906b2c03d7_173c5a84bb3e` | 2026-10-29 05:48:37 |
| Alibaba `_dlt_stage_daily_20261025_40eec796d5e4` | 2026-10-28 12:11:18 |
| Azure `_before_202610_warehouse_extract_fdlb7` | 2026-10-28 00:31:31 |

For example, the second AWS staging table reports API `expirationTime` =
`1793252917405` (2026-10-29 05:48:37.405 UTC). This is not an IAM approval expiry.
The checked `mart_cloud_cost.aws_member_daily_cost` table has **no** table TTL;
the short lifetimes are specific to the raw staging/backup artifacts above,
not all approved tables.

Therefore these objects cannot be recurring production sources, regardless of
the SA approval lasting until April 2027. Candidate stable names
`raw_aws_billing.cur_line_items`, `raw_aws_billing.aws_billing_20261001`,
`raw_alibaba_billing.daily_en_20261026` and
`raw_azure_billing.azure_billing_cost_20261001` returned metadata 403. This does
not distinguish missing objects from missing permission and is **not** proof of
a stable-source contract. The source owner must provide stable detail tables or
views (with month/date filtering, required attribution/resource fields and
continued read access), not new short-lived staging names.

Minimal source-owner request:

- Provide the exact stable AWS, Alibaba and Azure full-detail object names and
  schemas in `us-west1`, including export/billing partition lineage and how
  historical corrections are published.
- AWS must preserve nested or flattened tags, resource IDs, SKU/region/currency,
  account/payer, billing/usage dates and split parent/child columns. Alibaba and
  Azure must preserve owner/subscription identity, resource IDs and tags.
- Grant the existing target SA read/metadata access to those stable objects;
  table-specific grants are sufficient for named objects, without broadening a
  shared dashboard identity. The object lifecycle must outlast routine syncs
  and support future billing months.
- Source adapters must match the final shape. In particular, existing
  Azure/Alibaba wildcard importers rely on numeric table suffixes; a stable
  single table/view is not automatically a drop-in replacement for those
  wildcard queries.

## Cluster runtime identity verification

Read-only Kubernetes/IAM inspection and one read-only exec at 2026-10-27
08:35 UTC established:

- GCP's live CronJob uses KSA `apps/cost-insight-gcp-billing`, already annotated
  directly to the target warehouse GSA. The target policy has the matching
  `roles/iam.workloadIdentityUser` binding. There was no Running pod with this
  KSA, so fresh runtime ADC/query verification was not performed for it.
- AWS, F04, Azure and Alibaba live CronJobs use KSA `ci-dashboard`, mapped to
  `ci-dashboard@pingcap-testing-account.iam.gserviceaccount.com`.
- An existing Running pod using that shared KSA confirmed its real ADC GSA.
  Target-SA `testIamPermissions` returned no requested permissions, and a token
  refresh for impersonation returned **403: iam.serviceAccounts.getAccessToken**.
  No BigQuery query ran under that runtime target identity after the failure;
  downstream job/table permissions must not be inferred from this result.
- **User-confirmed identity plan (2026-10-27):** use the existing dedicated
  warehouse KSA `apps/cost-insight-gcp-billing` for warehouse-only billing jobs,
  directly bound to the target GSA. Do not expand the shared dashboard GSA's
  token-creation permissions. The shared-SA 403 is not a blocker to this chosen
  route; a fresh runtime smoke check of the dedicated route remains pending.
  This confirmation is recorded, **not applied to other CronJobs here**.
- A job directly bound to the target GSA must leave
  `COST_INSIGHT_BILLING_BQ_IMPERSONATE_SERVICE_ACCOUNT` **unset**, avoiding an
  unnecessary/unverified self-impersonation hop. Set billing project and region
  explicitly; do not change unrelated GCS/Tencent identities or regions.

The initial local ee-ops checkout was stale and differed from live state.
After fetching upstream on 2026-10-27, upstream `main` at `e0d237bb` already
contains the dedicated KSA/GSA binding, GCP warehouse project/raw source and
active F04 schedule. Do not propose those already-merged changes again or
reapply the older local manifest. Related OPS PR #2500 separately preserves a
GCP cutover partition boundary; this work does not alter that repair/guard.
No live cluster/IAM/OPS changes were made by the validation work.

## Azure and Alibaba

The new marts are useful comparison sources, not drop-in replacements for the
existing detail importers. The granted temporary Alibaba `_dlt_stage_daily_*`
and Azure `_before_*` tables retain detail fields, but should not be registered
as recurring production sources. Dataset table-list requests were denied, and
metadata requests for the candidate stable `daily_en_20261025` and
`azure_billing_cost_20261001` tables also returned 403 (absence cannot be
separated from lack of permission by that response).

## Local verification

- Full suite: **540 passed**, total coverage **90.94%** (90% gate passed).
- New billing client and warehouse relation modules: **100%** statement coverage.
- Existing default query fingerprints are preserved for all seven AWS builders.
- Focused lint for the client/config/exporter changes and new tests: passed.
- `git diff --check`: passed.
