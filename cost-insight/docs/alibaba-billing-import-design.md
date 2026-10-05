# Alibaba Cloud billing summary import

## Source validation

`gcp-digital-bi.alibaba_cloud.daily_en_*` is a daily-sharded, `partition_date`-partitioned
BigQuery export. Its relevant columns are `owner_account_id`, `partition_date`,
`usage_start_time`, product and instance dimensions, `instance_tag`, and the four amount
columns below.

The initial source is owner account `5028760335873601`
(`alicloud-testing-infra-dev`). August 2026 contains 22,403 rows and 31 export
partitions. Ten rows have a different `partition_date` and `DATE(usage_start_time)`, so
these values must remain separate rather than assuming a one-day export delay.

## Mapping

| Alibaba column | Cost Insight field |
| --- | --- |
| `owner_account_id` | `account_id`, `billing_account_id` |
| `partition_date` | `export_partition_date` |
| `DATE(usage_start_time)` | `usage_date` |
| `product_name` / `product_code`, `product_type`, `item`, `region`, `instance_id` / `instance_name` | service (with product-code fallback), SKU, usage type, region, resource dimensions (with instance-name fallback) |
| `instance_tag` | JSON `vendor_tags_json`; its `tenant` value is also `org` |
| `pretax_gross_amount` | `list_cost` |
| `amount_after_discount` | `effective_cost` |
| `-deducted_by_coupons` | `credit_amount` |
| `amount_after_discount - deducted_by_coupons` | `net_cost` |
| `payment_time` | `source_export_time` |

The destination summary schema is vendor-generic, so no DDL migration is required.
`usage_type` is retained as the deterministic `MIN(item)` display value, rather than a
summary identity dimension, matching the existing compact-ledger convention. Amounts are
rounded to its existing `DECIMAL(16,9)` precision. The August source
reconciles after summary aggregation to list/effective/credit/net totals of
1137.425035915 / 430.633880355 / -19.766020257 / 410.867860099 USD at that
target precision. The source amount columns are `FLOAT64`; aggregate comparisons
should use cents, not their unstable final fractional digits.

## Operation

The importer only accepts the `daily_en_*` table pattern, filters both wildcard suffix
and `partition_date`, and uses `owner_account_id` as the selected Cost Insight account.
It writes normal summary upserts, so reruns are idempotent; use explicit partition
replacement only for a source correction.

```bash
cost-insight sync-alibaba-billing-summary \
  --account-id 5028760335873601 \
  --export-partition-start 2026-08-01 \
  --export-partition-end 2026-08-31 \
  --earliest-usage-date 2026-08-01
```
