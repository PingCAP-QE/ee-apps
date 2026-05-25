# BigQuery Cost Optimization Design

## Context

The first Cost Insight collector imports GCP billing details from:

```text
gcp-digital-bi.gcp_billing_detailed.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6
```

The current collector reads the detailed billing export, groups by daily cost
dimensions including `resource_name`, writes `cost_raw_details`, then rebuilds
`cost_attribution_daily` from TiDB. This is operationally simple, but it makes
the regular collector pay for resource-level BigQuery reads even though most
dashboard views only need summary attribution.

The main optimization target is BigQuery query scan cost. TiDB write cost,
TiDB storage, and dashboard query latency are out of scope for this design.

## Current Query Cost Findings

Dry runs were executed against the current billing export table on
2026-05-25. Dry runs do not execute the query, but BigQuery returns precise
`totalBytesProcessed` estimates for scan cost.

The table is day-partitioned, but `bq show` reports no partition field:

```json
"timePartitioning": {
  "type": "DAY"
}
```

That means the table behaves like an ingestion-time partitioned table. The
current collector filters only by `DATE(usage_start_time)`, which does not
prune ingestion-time partitions.

| Query shape | Date filters | Bytes processed |
| --- | --- | ---: |
| Current full resource-level query, one usage day | `DATE(usage_start_time) = 2026-05-17` only | 439,825,517,869 |
| Current full resource-level query, one usage day | `_PARTITIONDATE = 2026-05-17` and usage day | 3,617,177,644 |
| Summary attribution query, one usage day | `_PARTITIONDATE = 2026-05-17` and usage day | 2,449,889,460 |
| Summary attribution query, 7 usage days | `_PARTITIONDATE` = same 7 days | 23,821,579,733 |
| Full resource-level query, 7 usage days | `_PARTITIONDATE` = same 7 days | 33,882,272,158 |
| Summary attribution query, 7 usage days plus 5-day export lag window | `_PARTITIONDATE` = 13 days, usage = 7 days; useful for a usage-window design, not the recommended export-partition incremental path | 41,149,018,219 |
| Resource-level unmatched query, 7 usage days plus 5-day export lag window | `_PARTITIONDATE` = 13 days, usage = 7 days | 51,821,083,446 |
| Summary attribution correction query, 30 usage days | `_PARTITIONDATE` = same 30 days | 86,409,750,092 |

The biggest cost issue is missing partition pruning, not TiDB storage
granularity. Field trimming helps, but only after the collector prunes
`_PARTITIONDATE`.

## Product Requirements

1. Cost dashboard views do not need yesterday-level freshness.
2. Normal trend, repo, group, and budget views need accurate summary cost.
3. Resource-level unmatched data should still refresh weekly, because resource
   investigation loses context when it is delayed too long.
4. Historical billing corrections should update summary cost.
5. Historical billing corrections do not need resource-level detail.

## Design Goals

- Scan every BigQuery export partition at most once for the regular summary
  pipeline.
- Avoid resource-level columns in the regular summary pipeline.
- Preserve service and SKU dimensions in the regular summary pipeline so
  dashboard attribution can still answer service-level cost questions without
  reading deprecated raw detail tables.
- Apply source-side owner overrides for cost lines that cannot be labeled on
  GCP resources. For now, unlabeled Cloud Logging and
  `Compute Flexible Committed Use Discounts - 3 Year` rows are assigned to
  `author=wei_zheng`, which fuzzy-matches the `wei.zheng@pingcap.com` roster
  identity and maps to the EQ roster group during attribution.
- Keep resource-level unmatched data as a separate weekly pipeline.
- Make late-arriving usage and billing corrections additive and idempotent.
- Keep dashboard APIs mostly backed by `cost_attribution_daily`.

## Proposed Data Model

### `cost_bq_export_summary_daily`

This table stores additive summary facts by BigQuery export partition. It is
the durable import layer for regular cost summaries.

```sql
CREATE TABLE cost_bq_export_summary_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  billing_account_id VARCHAR(128) NULL,
  export_partition_date DATE NOT NULL,
  usage_date DATE NOT NULL,
  service_name VARCHAR(255) NULL,
  sku_name VARCHAR(255) NULL,
  org VARCHAR(255) NULL,
  repo VARCHAR(255) NULL,
  author VARCHAR(255) NULL,
  list_cost DECIMAL(16, 2) NULL,
  effective_cost DECIMAL(16, 2) NULL,
  credit_amount DECIMAL(16, 2) NULL,
  net_cost DECIMAL(16, 2) NULL,
  source_export_time DATETIME NULL,
  source_row_hash CHAR(64) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_bq_export_summary_source_row (
    vendor,
    account_id,
    export_partition_date,
    source_row_hash
  ),
  KEY idx_cost_bq_export_summary_usage_date (usage_date, vendor, account_id),
  KEY idx_cost_bq_export_summary_export_partition (export_partition_date)
);
```

Important behavior:

- `export_partition_date` comes from BigQuery `_PARTITIONDATE`.
- The job watermark advances by export partition, not by usage date.
- A late billing row for old `usage_date` lands in a newer
  `export_partition_date`; the next regular summary import captures it without
  rescanning older partitions.
- Queries for final actual cost sum all rows by `usage_date`, regardless of
  export partition.
- `source_row_hash` should include `export_partition_date`, `usage_date`,
  account, billing account, and normalized owner dimensions. It should not
  include amounts or `source_export_time`.
- Hash fields:
  - `vendor`
  - `account_id`
  - `billing_account_id`
  - `export_partition_date`
  - `usage_date`
  - `author`
  - `org`
  - `repo`

This table is intentionally summary-only. It should not include
`resource_name`, `service_name`, `sku_name`, `region`, or `usage_seconds` unless
a dashboard requirement needs those columns in the regular path.

### `cost_unmatched_resource_daily`

This table stores the weekly resource-level investigation data.

```sql
CREATE TABLE cost_unmatched_resource_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  billing_account_id VARCHAR(128) NULL,
  export_partition_date DATE NOT NULL,
  usage_date DATE NOT NULL,
  service_name VARCHAR(255) NULL,
  sku_name VARCHAR(255) NULL,
  namespace VARCHAR(255) NULL,
  org VARCHAR(255) NULL,
  repo VARCHAR(255) NULL,
  author VARCHAR(255) NULL,
  resource_name VARCHAR(512) NOT NULL,
  usage_seconds DECIMAL(20, 2) NULL,
  list_cost DECIMAL(16, 2) NULL,
  effective_cost DECIMAL(16, 2) NULL,
  credit_amount DECIMAL(16, 2) NULL,
  net_cost DECIMAL(16, 2) NULL,
  source_export_time DATETIME NULL,
  source_row_hash CHAR(64) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_unmatched_resource_source_row (
    vendor,
    account_id,
    export_partition_date,
    source_row_hash
  ),
  KEY idx_cost_unmatched_resource_usage_date (usage_date, vendor, account_id),
  KEY idx_cost_unmatched_resource_resource_name (resource_name(255)),
  KEY idx_cost_unmatched_resource_repo (usage_date, org, repo)
);
```

This table is allowed to read resource-level BigQuery columns, but only in the
weekly unmatched-resource job. Historical correction jobs should not update
this table.

Hash fields:

- `vendor`
- `account_id`
- `billing_account_id`
- `export_partition_date`
- `usage_date`
- `service_name`
- `sku_name`
- `namespace`
- `author`
- `org`
- `repo`
- `resource_name`

The current `cost_raw_details` table can be kept temporarily for compatibility,
but it should stop being the regular collector's primary output. New dashboard
code should read `cost_unmatched_resource_daily` for the Top unmatched resources
panel.

`usage_seconds` should stay in this table. It is useful investigation context,
and the resource-level query already accepts the extra usage-field scan cost
because this job is intentionally limited to the weekly unmatched window.

## BigQuery Query Shapes

### Regular Summary Import

Scan new export partitions only:

```sql
SELECT
  'gcp' AS vendor,
  project.id AS account_id,
  billing_account_id,
  _PARTITIONDATE AS export_partition_date,
  DATE(usage_start_time) AS usage_date,
  ARRAY(
    SELECT label.value
    FROM UNNEST(labels) AS label
    WHERE label.key IN ('k8s-label/author', 'author')
    LIMIT 1
  )[SAFE_OFFSET(0)] AS author,
  ARRAY(
    SELECT label.value
    FROM UNNEST(labels) AS label
    WHERE label.key IN ('k8s-label/org', 'org')
    LIMIT 1
  )[SAFE_OFFSET(0)] AS org,
  ARRAY(
    SELECT label.value
    FROM UNNEST(labels) AS label
    WHERE label.key IN ('k8s-label/repo', 'repo')
    LIMIT 1
  )[SAFE_OFFSET(0)] AS repo,
  ROUND(SUM(cost_at_list), 2) AS list_cost,
  ROUND(SUM(cost), 2) AS effective_cost,
  ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 2)
    AS credit_amount,
  ROUND(SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 2)
    AS net_cost,
  MAX(export_time) AS source_export_time
FROM `gcp-digital-bi.gcp_billing_detailed.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6`
WHERE _PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end
  AND project.id = @account_id
  AND DATE(usage_start_time) >= @earliest_usage_date
GROUP BY
  account_id,
  billing_account_id,
  export_partition_date,
  usage_date,
  author,
  org,
  repo;
```

The regular job should not restrict usage date to a single week. It should scan
new export partitions and accept any relevant `usage_date` inside those
partitions. This captures late-arriving usage and correction rows without
rescanning old partitions.

### Weekly Unmatched Resource Import

Scan resource-level columns only for the latest stable investigation window:

```sql
SELECT
  'gcp' AS vendor,
  project.id AS account_id,
  billing_account_id,
  _PARTITIONDATE AS export_partition_date,
  DATE(usage_start_time) AS usage_date,
  service.description AS service_name,
  sku.description AS sku_name,
  ARRAY(
    SELECT label.value
    FROM UNNEST(labels) AS label
    WHERE label.key IN ('k8s-namespace', 'namespace')
    LIMIT 1
  )[SAFE_OFFSET(0)] AS namespace,
  ARRAY(
    SELECT label.value
    FROM UNNEST(labels) AS label
    WHERE label.key IN ('k8s-label/author', 'author')
    LIMIT 1
  )[SAFE_OFFSET(0)] AS author,
  ARRAY(
    SELECT label.value
    FROM UNNEST(labels) AS label
    WHERE label.key IN ('k8s-label/org', 'org')
    LIMIT 1
  )[SAFE_OFFSET(0)] AS org,
  ARRAY(
    SELECT label.value
    FROM UNNEST(labels) AS label
    WHERE label.key IN ('k8s-label/repo', 'repo')
    LIMIT 1
  )[SAFE_OFFSET(0)] AS repo,
  COALESCE(
    ARRAY(
      SELECT label.value
      FROM UNNEST(labels) AS label
      WHERE label.key IN ('k8s-workload-name')
      LIMIT 1
    )[SAFE_OFFSET(0)],
    NULLIF(resource.name, ''),
    NULLIF(resource.global_name, '')
  ) AS resource_name,
  CASE
    WHEN COUNTIF(
      LOWER(usage.pricing_unit) IS NULL
      OR LOWER(usage.pricing_unit) NOT IN ('hour', 'minute', 'second')
    ) > 0 THEN NULL
    WHEN COUNTIF(LOWER(usage.pricing_unit) = 'hour') = COUNT(*)
      THEN ROUND(SUM(usage.amount_in_pricing_units) * 3600, 2)
    WHEN COUNTIF(LOWER(usage.pricing_unit) = 'minute') = COUNT(*)
      THEN ROUND(SUM(usage.amount_in_pricing_units) * 60, 2)
    WHEN COUNTIF(LOWER(usage.pricing_unit) = 'second') = COUNT(*)
      THEN ROUND(SUM(usage.amount_in_pricing_units), 2)
    ELSE NULL
  END AS usage_seconds,
  ROUND(SUM(cost_at_list), 2) AS list_cost,
  ROUND(SUM(cost), 2) AS effective_cost,
  ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 2)
    AS credit_amount,
  ROUND(SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)), 2)
    AS net_cost,
  MAX(export_time) AS source_export_time
FROM `gcp-digital-bi.gcp_billing_detailed.gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6`
WHERE _PARTITIONDATE BETWEEN @export_partition_start AND @export_partition_end
  AND project.id = @account_id
  AND DATE(usage_start_time) BETWEEN @usage_start_date AND @usage_end_date
GROUP BY
  account_id,
  billing_account_id,
  export_partition_date,
  usage_date,
  service_name,
  sku_name,
  namespace,
  author,
  org,
  repo,
  resource_name;
```

The final unmatched-resource filter can happen in TiDB after attribution, so
the BigQuery query does not need to perfectly know whether the author matches a
roster employee. The important BigQuery cost boundary is that this query runs
weekly and never participates in historical correction backfills.

## Attribution Rebuild From Summary

The existing attribution refresh reads `cost_raw_details` and groups by
`service_name`, `sku_name`, and `resource_name`. The summary path cannot reuse
that SQL unchanged because `cost_bq_export_summary_daily` intentionally omits
resource-level columns, while keeping the service and SKU dimensions.

The implementation should either add a new
`refresh-cost-attribution-from-summary` command or parameterize the existing
refresh job with a source table mode. The summary mode should rebuild
`cost_attribution_daily` with `service_name` and `sku_name` from the summary
rows and `resource_name` set to `NULL`.

The summary attribution query shape is:

```sql
INSERT INTO cost_attribution_daily (
  usage_date,
  vendor,
  account_id,
  service_name,
  sku_name,
  org,
  repo,
  resource_name,
  author,
  owner,
  attribution_key,
  attribution_source,
  attribution_status,
  employee_id,
  group_id,
  manager_id,
  usage_seconds,
  list_cost,
  effective_cost,
  credit_amount,
  net_cost,
  source_rows,
  dimension_hash
)
SELECT
  attributed.usage_date,
  attributed.vendor,
  attributed.account_id,
  attributed.service_name,
  attributed.sku_name,
  attributed.org,
  attributed.repo,
  NULL AS resource_name,
  attributed.author,
  attributed.owner,
  attributed.attribution_key,
  attributed.attribution_source,
  attributed.attribution_status,
  attributed.employee_id,
  attributed.group_id,
  attributed.manager_id,
  NULL AS usage_seconds,
  SUM(attributed.list_cost) AS list_cost,
  SUM(attributed.effective_cost) AS effective_cost,
  SUM(attributed.credit_amount) AS credit_amount,
  SUM(attributed.net_cost) AS net_cost,
  COUNT(*) AS source_rows,
  SHA2(
    CONCAT_WS(
      '|',
      DATE_FORMAT(attributed.usage_date, '%Y-%m-%d'),
      COALESCE(attributed.vendor, ''),
      COALESCE(attributed.account_id, ''),
      '',
      '',
      COALESCE(attributed.org, ''),
      COALESCE(attributed.repo, ''),
      '',
      COALESCE(attributed.author, ''),
      COALESCE(attributed.owner, ''),
      COALESCE(attributed.attribution_key, ''),
      COALESCE(attributed.attribution_source, ''),
      COALESCE(attributed.attribution_status, ''),
      COALESCE(CAST(attributed.employee_id AS CHAR), ''),
      COALESCE(CAST(attributed.group_id AS CHAR), ''),
      COALESCE(CAST(attributed.manager_id AS CHAR), '')
    ),
    256
  ) AS dimension_hash
FROM (
  SELECT
    summary.usage_date,
    summary.vendor,
    summary.account_id,
    summary.org,
    summary.repo,
    summary.author,
    summary.author AS owner,
    CASE
      WHEN COALESCE(
        github_employee.id,
        email_employee.id,
        normalized_employee.id
      ) IS NOT NULL THEN CONCAT(
        'employee:',
        CAST(COALESCE(
          github_employee.id,
          email_employee.id,
          normalized_employee.id
        ) AS CHAR)
      )
      WHEN summary.author IS NOT NULL THEN CONCAT('author:', LOWER(summary.author))
      ELSE 'unattributed'
    END AS attribution_key,
    CASE
      WHEN github_employee.id IS NOT NULL THEN 'author_github'
      WHEN email_employee.id IS NOT NULL THEN 'author_email'
      WHEN normalized_employee.id IS NOT NULL THEN 'author_normalized'
      WHEN summary.author IS NOT NULL THEN 'author_label'
      ELSE 'missing_author'
    END AS attribution_source,
    CASE
      WHEN COALESCE(
        github_employee.id,
        email_employee.id,
        normalized_employee.id
      ) IS NOT NULL THEN 'matched'
      WHEN summary.author IS NOT NULL THEN 'unmatched'
      ELSE 'unattributed'
    END AS attribution_status,
    COALESCE(
      github_employee.id,
      email_employee.id,
      normalized_employee.id
    ) AS employee_id,
    COALESCE(
      github_employee.group_id,
      email_employee.group_id,
      normalized_employee.group_id
    ) AS group_id,
    COALESCE(
      github_employee.manager_id,
      email_employee.manager_id,
      normalized_employee.manager_id,
      matched_group.manager_id
    ) AS manager_id,
    summary.list_cost,
    summary.effective_cost,
    summary.credit_amount,
    summary.net_cost
  FROM cost_bq_export_summary_daily summary
  LEFT JOIN roster_employees github_employee
    ON github_employee.is_active = 1
   AND summary.author IS NOT NULL
   AND github_employee.github_id IS NOT NULL
   AND LOWER(github_employee.github_id) = LOWER(summary.author)
  LEFT JOIN roster_employees email_employee
    ON github_employee.id IS NULL
   AND email_employee.is_active = 1
   AND summary.author IS NOT NULL
   AND email_employee.email IS NOT NULL
   AND (
     LOWER(email_employee.email) = LOWER(summary.author)
     OR LOWER(SUBSTRING_INDEX(email_employee.email, '@', 1)) = LOWER(summary.author)
   )
  LEFT JOIN roster_employees normalized_employee
    ON github_employee.id IS NULL
   AND email_employee.id IS NULL
   AND normalized_employee.is_active = 1
   AND summary.author IS NOT NULL
   AND (
     normalized_employee.github_id IS NOT NULL
     OR normalized_employee.email IS NOT NULL
     OR normalized_employee.en_name IS NOT NULL
   )
   AND (
     <normalized_summary_author> = <normalized_github_id>
     OR <normalized_summary_author> = <normalized_email_local>
     OR <normalized_summary_author> = <normalized_en_name>
   )
  LEFT JOIN roster_groups matched_group
    ON matched_group.is_active = 1
   AND matched_group.id = COALESCE(
     github_employee.group_id,
     email_employee.group_id,
     normalized_employee.group_id
   )
  WHERE summary.usage_date BETWEEN @start_date AND @end_date
    AND summary.vendor = @vendor
    AND summary.account_id = @account_id
) attributed
GROUP BY
  attributed.usage_date,
  attributed.vendor,
  attributed.account_id,
  attributed.org,
  attributed.repo,
  attributed.author,
  attributed.owner,
  attributed.attribution_key,
  attributed.attribution_source,
  attributed.attribution_status,
  attributed.employee_id,
  attributed.group_id,
  attributed.manager_id;
```

The placeholder normalized expressions should reuse the existing
`normalized_identity_sql()` helper. This keeps author matching behavior aligned
with the current raw-detail attribution job.

After a summary import, touched usage dates should be discovered from the
summary table, not inferred from the export partition window:

```sql
SELECT DISTINCT usage_date
FROM cost_bq_export_summary_daily
WHERE export_partition_date BETWEEN @export_partition_start AND @export_partition_end
  AND vendor = @vendor
  AND account_id = @account_id
ORDER BY usage_date;
```

The refresh command can then rebuild those dates one by one, or merge
contiguous dates into ranges. One-by-one refresh remains the safer default for
TiDB memory usage.

## Refresh Schedule

### Recommended Defaults

| Setting | Default | Meaning |
| --- | ---: | --- |
| `COST_INSIGHT_SYNC_INTERVAL` | weekly | Run the regular summary import once per week |
| `COST_INSIGHT_SYNC_LAG_DAYS` | 5 | Only import export partitions up to `today - 5 days` |
| `COST_INSIGHT_EXPORT_OVERLAP_DAYS` | 0 or 1 | Safety overlap on export partitions, not usage dates |
| `COST_INSIGHT_SYNC_INITIAL_LOOKBACK_DAYS` | unset | Optional bootstrap window for first run |
| `COST_INSIGHT_UNMATCHED_RESOURCE_INTERVAL` | weekly | Refresh resource investigation data weekly |
| `COST_INSIGHT_UNMATCHED_RESOURCE_LAG_DAYS` | 5 | Use the same stable cutoff as regular summary import |

### Regular Weekly Summary Job

1. Read `cost_job_state` for the last imported export partition.
2. Resolve `export_partition_end = today - sync_lag_days`.
3. Resolve `export_partition_start = last_export_partition + 1 - overlap`.
4. Query BigQuery with `_PARTITIONDATE BETWEEN export_partition_start AND
   export_partition_end`.
5. Upsert `cost_bq_export_summary_daily`.
6. Rebuild or incrementally refresh `cost_attribution_daily` for every
   `usage_date` touched by the imported rows.
7. Advance the watermark only after TiDB writes and attribution refresh
   complete.

If there is no prior watermark, the job should require either an explicit
`--export-partition-start` value or `COST_INSIGHT_SYNC_INITIAL_LOOKBACK_DAYS`.
When the initial lookback is unset, the automatic first run should import only
the single stable export partition at `today - sync_lag_days`; full history
should be loaded by an explicit manual backfill.

For migration, historical rows can be backfilled from the existing TiDB
`cost_raw_details` table instead of querying BigQuery again:

```bash
cost-insight backfill-gcp-cost-refine-from-raw \
  --start-date 2026-01-01 \
  --end-date 2026-05-20 \
  --mark-summary-watermark
```

This local backfill aggregates raw rows into `cost_bq_export_summary_daily` and
copies grouped resource rows into `cost_unmatched_resource_daily`.
`cost_raw_details` does not store BigQuery `_PARTITIONDATE`, so the backfill
uses `DATE(source_export_time)` as a synthetic `export_partition_date`, with
`usage_date` as a fallback for older/null rows. `--mark-summary-watermark`
advances the new summary importer state through the synthetic export partition
end so the normal job starts from new export partitions after the cutover.

### Weekly Unmatched Resource Job

1. Resolve the latest complete stable usage week.
2. Set `_PARTITIONDATE` to cover the usage week plus lag window.
3. Query resource-level data and upsert `cost_unmatched_resource_daily`.
4. Rebuild the dashboard unmatched-resource materialization for that usage
   week.

### Historical Corrections

No routine 30- or 45-day resource-level correction is needed.

Because regular summary import is export-partition incremental, late usage and
correction rows are naturally captured from new export partitions and added to
the summary table. A separate monthly reconciliation job can still exist as a
safety net, but it should be summary-only and should be used for validation or
missed-partition repair, not as the normal correction path.

If implemented, monthly reconciliation should be a separate CLI command such
as `reconcile-gcp-billing-summary`. It should never call the resource-level
unmatched importer.

## Concurrency Model

The initial implementation should assume one active instance per cost job. A
job should check `cost_job_state.last_status` before starting and refuse to run
when the same job is already `running`, unless a force flag is provided for
manual recovery.

The summary importer and unmatched importer write different tables and can run
independently. Attribution refresh should not overlap with another attribution
refresh for the same vendor, account, and usage date range. The simplest safe
rule is to have the summary importer own the summary attribution refresh inside
one job flow, and schedule unmatched resource refresh at a different time.

## Dashboard Query Changes

Current dashboard behavior:

- `cost-trend` reads `cost_attribution_daily`.
- `cost-repo-group-stack` reads `cost_attribution_daily`.
- `cost-engineering-group-share` reads `cost_attribution_daily` and
  `roster_groups`.
- `cost-unmatched-resources` reads both `cost_attribution_daily` and
  `cost_raw_details`.

Target dashboard behavior:

- Keep trend, repo stack, and group share on `cost_attribution_daily`.
- Move Top unmatched resource details from `cost_raw_details` to
  `cost_unmatched_resource_daily`.
- Keep `cost_attribution_daily` as the source of attribution status. The panel
  should join unmatched attribution rows to resource detail rows instead of
  treating every row in `cost_unmatched_resource_daily` as unmatched.
- Use an inner join for that panel. `cost_unmatched_resource_daily` is weekly
  resource context, not a roster-aware unmatched table; showing rows without
  matching `cost_attribution_daily` dimensions would create false positives.
  The intended run order is summary import, attribution refresh, then weekly
  resource import before exposing that week's resource details.
- Keep `cost_raw_details` only for compatibility or one-off debugging during
  migration.

The target query shape is:

```sql
WITH unmatched_dimensions AS (
  SELECT
    c.usage_date,
    c.vendor,
    c.account_id,
    c.org,
    c.repo,
    c.author,
    MAX(c.attribution_key) AS attribution_key,
    MAX(c.attribution_source) AS attribution_source,
    MAX(c.attribution_status) AS attribution_status
  FROM cost_attribution_daily c
  WHERE c.usage_date BETWEEN @start_date AND @end_date
    AND c.vendor = @vendor
    AND c.account_id = @account_id
    AND c.employee_id IS NULL
  GROUP BY c.usage_date, c.vendor, c.account_id, c.org, c.repo, c.author
),
unallocated_resource_rows AS (
  SELECT
    r.resource_name,
    r.service_name,
    r.sku_name,
    r.org AS org_name,
    r.repo AS repo_name,
    r.author AS author_name,
    r.usage_date,
    r.namespace,
    r.usage_seconds,
    r.list_cost,
    u.attribution_key,
    u.attribution_source,
    u.attribution_status
  FROM cost_unmatched_resource_daily r
  JOIN unmatched_dimensions u
    ON r.usage_date = u.usage_date
   AND r.vendor = u.vendor
   AND r.account_id = u.account_id
   AND (r.org = u.org OR (r.org IS NULL AND u.org IS NULL))
   AND (r.repo = u.repo OR (r.repo IS NULL AND u.repo IS NULL))
   AND (r.author = u.author OR (r.author IS NULL AND u.author IS NULL))
  WHERE r.usage_date BETWEEN @start_date AND @end_date
    AND r.vendor = @vendor
    AND r.account_id = @account_id
    AND r.resource_name IS NOT NULL
    AND r.resource_name <> ''
    AND (
      r.namespace IS NULL
      OR r.namespace IN (
        'kube:unallocated',
        'kube:system-overhead',
        'goog-k8s-unsupported-sku',
        'goog-k8s-unknown'
      )
    )
),
resource_details AS (
  SELECT
    resource_name,
    MAX(service_name) AS service_name,
    MAX(sku_name) AS sku_name,
    MAX(org_name) AS org_name,
    MAX(repo_name) AS repo_name,
    MAX(author_name) AS author_name,
    MAX(attribution_key) AS attribution_key,
    MAX(attribution_source) AS attribution_source,
    MAX(attribution_status) AS attribution_status,
    MIN(usage_date) AS first_seen_date,
    MAX(usage_date) AS last_seen_date,
    GROUP_CONCAT(DISTINCT COALESCE(namespace, '<null>')) AS allocation_buckets,
    SUM(COALESCE(usage_seconds, 0)) AS usage_seconds,
    SUM(list_cost) AS list_cost
  FROM unallocated_resource_rows
  GROUP BY resource_name
)
SELECT *
FROM resource_details
ORDER BY list_cost DESC, resource_name
LIMIT @limit;
```

This preserves the current dashboard semantics: attribution status comes from
the roster-aware attribution table, while resource investigation context comes
from the weekly resource table.

## Expected Cost Impact

Using the dry-run measurements above:

Current collector:

```text
439.8 GB per run * 7 daily runs = about 3.08 TB/week
```

Target weekly jobs with partition pruning:

```text
Regular summary, 7 new export partitions: about 23.8 GB/week
Unmatched resource, 7 export partitions: about 30.0 GB/week
Total without lag window: about 53.8 GB/week
```

The recommended regular summary path is export-partition incremental. The
5-day lag delays the export-partition cutoff, but once the job is caught up it
still scans about 7 new export partitions per weekly run. The unmatched
resource job is different: it answers a usage-window investigation question, so
it should scan the usage week plus the lag window.

With a 5-day lag window for the weekly unmatched-resource usage window:

```text
Regular summary: about 23.8 GB/week
Unmatched resource: about 51.8 GB/week
Total with weekly unmatched lag window: about 75.6 GB/week
```

An optional monthly summary-only reconciliation using the measured 30-day
summary query would add about 86.4 GB/month, or about 21.6 GB/week amortized:

```text
Regular summary + weekly unmatched + monthly summary reconciliation:
23.8 GB + 51.8 GB + 21.6 GB = about 97.2 GB/week
```

This is roughly 2.5% to 3.2% of the current weekly scan volume if compared with the
current no-partition-pruning collector. Even if a safety overlap or monthly
summary reconciliation is added, the expected scan volume should remain far
below the current daily resource-level collector.

At BigQuery on-demand pricing, scan cost is proportional to bytes processed, so
the actual dollar savings should track these byte reductions. The exact price
depends on the account's BigQuery pricing model and free-tier/reservation
status.

## Migration Plan

1. Add SQL migrations for `cost_bq_export_summary_daily` and
   `cost_unmatched_resource_daily`.
2. Add `sync-gcp-billing-summary`:
   - export-partition watermark
   - `_PARTITIONDATE` filter
   - summary-only fields
   - touched-usage-date output
3. Add `refresh-cost-attribution-from-summary` or refactor the existing refresh
   job to read `cost_bq_export_summary_daily`:
   - set `service_name`, `sku_name`, `resource_name`, and `usage_seconds` to
     `NULL`
   - reuse the existing roster matching joins and normalized identity helper
   - discover touched usage dates from `cost_bq_export_summary_daily`
   - refresh touched dates one by one by default
4. Add `sync-gcp-unmatched-resources`:
   - weekly stable usage window
   - resource-level fields
   - no historical correction mode by default
5. Change `cost-unmatched-resources` API to read
   `cost_unmatched_resource_daily` joined with `cost_attribution_daily`.
6. Add `reconcile-gcp-billing-summary` as an optional summary-only validation
   and repair command.
7. Keep the existing `sync-gcp-billing-export` raw collector as a manual
   fallback during rollout, then deprecate it once the new path is verified.
8. Add job-level running-state checks based on `cost_job_state`.
9. Add dry-run guardrails to CLI output so operators can see estimated
   BigQuery bytes before running large backfills.

## Open Questions

- Should regular summary keep `service_name` and `sku_name` for future product
  views? Current dashboard pages do not need them. Adding them increases
  BigQuery bytes and output cardinality, so the default should be no.
- Should `cost_attribution_daily` keep `service_name`, `sku_name`, and
  `resource_name` nullable under the summary path, or should a new
  `cost_attribution_summary_daily` table be introduced? Reusing
  `cost_attribution_daily` is simpler for the dashboard.
- What is the exact production CronJob schedule in `ee-ops`? This repository
  does not currently contain a cost-insight CronJob manifest.
