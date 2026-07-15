# tcms 资源逻辑分账设计

> 状态：实现中。tcms 手动建库/建表/grant SQL 见 `docs/tcms-resource-allocation-setup.sql`。
> 关联：`docs/system-design.md`（现有 cost 数据系统设计）、飞书 [WIP] Cost Dashboard 分账体系设计 3.3 节。

## 1. 背景与目标

cost-insight 现在主要把 AWS `tag_used_by` 映射到内部 `author` 字段，并 join `roster_employees` 把成本归属到
人/team。tcms（test infra app）创建的一批 AWS 资源打不上内部分账 label，但 billing 里能看到
TiDB Cloud 相关 vendor tag，例如 AWS console 的 `shared-pool` 在 CUR/staging 中表现为
`resource_tags.key_value.key='user_shared_pool'`，逻辑集群为 `tag_cluster`。

目标：tcms 写一张事实表，cost 只读 join，把这类资源分账到 `owner_email/service/project/service_exec_id`；
物理集群固定成本按同一 shared pool 下 logical cluster 的 project net_cost 占比分摊。

## 2. 已确认决策

1. tcms 事实表建在独立 db：`tcms_cost.resource_allocation`。tcms 直接写，cost 跨库只读。
2. vendor tag 不再拆成固定列，统一存 `vendor_tags_json`。当前 JSON key 为 `shared_pool` 和 `cluster`，后续可扩展。
   JSON 中只保存有值的 tag，key 缺失表示不约束该 tag；例如 `{"shared_pool":"a"}` 可匹配所有
   shared_pool=a 的资源。不要写 JSON null/空字符串来表达 wildcard。
3. TCMS label 字段去掉 `i_` 前缀：`owner_email/service/project/service_exec_id`。
4. Cost 结果不新建表，直接扩展 `cost_attribution_daily`。
5. TCMS `vendor_tags_json` 命中优先于 legacy `tag_used_by`/`author`；未命中 TCMS 时再走原 author roster 逻辑。
6. 按 `vendor_tags_json` 做包含关系匹配：allocation tags 必须是 billing tags 的子集；
   若多条 allocation 命中，按匹配 key 数量选择最具体的一条。
7. shared pool 固定成本：billing 行无 author、`cluster` 为空、`shared_pool` 非空；按同一 shared pool 下各
   `(service, project)` logical net_cost 占比分摊。
8. fallback 必须守恒：tcms 表为空、未命中 allocation、或 pool 无 logical 行时，成本保留为 unattributed。

## 3. tcms 事实表

执行版 DDL 在 `docs/tcms-resource-allocation-setup.sql`。核心 schema：

```sql
CREATE TABLE tcms_cost.resource_allocation (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NULL,
  vendor_tags_json JSON NOT NULL,
  owner_email VARCHAR(255) NULL,
  service VARCHAR(255) NULL,
  project VARCHAR(255) NULL,
  service_exec_id VARCHAR(255) NULL,
  valid_from DATE NULL,
  valid_to DATE NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_resource_allocation_lookup (vendor, account_id, valid_from, valid_to),
  KEY idx_resource_allocation_owner (owner_email),
  KEY idx_resource_allocation_project (project)
);
```

示例：

```json
{"cluster":"10149878793099322221","shared_pool":"2076551309477019648"}
```

约束由 tcms 写入侧负责：

- `vendor_tags_json` 至少包含一个用于匹配的 vendor tag，只写有值的 tag。
  缺失 key 表示 wildcard，不要写 JSON null/空字符串来表达 wildcard；
  pool 级记录写 `{"shared_pool":"..."}`，不要写 `{"cluster":null,"shared_pool":"..."}`。
- 同 `(vendor, account_id, vendor_tags_json)` 的 `valid_from/valid_to` 区间不重叠。
- 同一 tag 组合不要同时存在 account 精确行和 `account_id IS NULL` 泛化行，避免 cost join 双命中。

## 4. cost 侧表扩展

```sql
ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS vendor_tags_json JSON NULL AFTER target_branch;

ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN IF NOT EXISTS vendor_tags_json JSON NULL AFTER target_branch;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS vendor_tags_json JSON NULL AFTER resource_name,
  ADD COLUMN IF NOT EXISTS service VARCHAR(255) NULL AFTER owner,
  ADD COLUMN IF NOT EXISTS project VARCHAR(255) NULL AFTER service,
  ADD COLUMN IF NOT EXISTS service_exec_id VARCHAR(255) NULL AFTER project,
  ADD COLUMN IF NOT EXISTS allocate_method VARCHAR(32) NULL AFTER attribution_status;
```

AWS BigQuery query 从 staging 中抽取：

```sql
CASE
  WHEN shared_pool IS NULL AND `cluster` IS NULL THEN NULL
  ELSE TO_JSON_STRING(STRUCT(`cluster` AS cluster, shared_pool AS shared_pool))
END AS vendor_tags_json
```

其中 `shared_pool` 来自 nested `resource_tags.key_value` 的 `user_shared_pool`；`cluster` 来自扁平字段
`tag_cluster`。已确认 account `946646677266` 近 30 天 staging key 是 `user_shared_pool`，不是
AWS console 上看到的 `shared-pool`。
sync 写入 TiDB 前会规范化 JSON，去掉 null/empty key，因此 shared_pool-only 行最终存为
`{"shared_pool":"a"}`。

## 5. 分账流程

INSERT A（非 shared 固定成本）：

- tcms subset match 命中：`owner=allocation.owner_email`，即使 `tag_used_by`/内部 `author` 非空也优先使用 TCMS；
  `service/project/service_exec_id/vendor_tags_json` 填入。匹配条件包含 `cluster` 时 `allocate_method='logical'`；
  只按 pool/tag 泛化命中时 `allocate_method='vendor_tag'`。
- tcms 未命中、author 非空：按原 author roster 逻辑归属，`vendor_tags_json/service/project/service_exec_id` 为空。
- tcms 未命中、author 为空、JSON `cluster` 非空：保留为 `missing_label_allocation/unattributed`。
- tcms 未命中、无 author、无 `cluster`、无 `shared_pool`：保留旧 missing author 行。
- 若 `{"shared_pool":"a","cluster":"x"}` 和 `{"shared_pool":"a"}` 都能匹配同一 billing row，
  先选 key 更多的前者；后者只会匹配剩余没有更具体命中的 shared_pool=a 成本。

INSERT B（shared pool 固定成本）：

- logical 权重：同一 `(usage_date, vendor, account_id, shared_pool)` 下，按 `(service, project)` logical
  `SUM(net_cost)` 计算占比。
- shared cost：无 author、JSON `cluster` 为空、JSON `shared_pool` 非空，且没有被
  `{"shared_pool":"..."}` 这类泛化 allocation 直接命中。
- 有 logical 权重：四列成本都按同一占比分摊，`attribution_status='shared'`，
  `allocate_method='shared_weighted'`。
- 无 logical 权重：整池 shared cost 原样写成 `missing_label_allocation/unattributed`，保证总成本守恒。
- `pool_logical_cost <= 0` 时 fallback 为按 allocation count 均分，避免除 0 或负比例。

## 6. 兼容性与重算

- `resource_allocation` 可以为空：TCMS 未写入数据时，author 行仍归属，其他 JSON tag 行 fallback 到 unattributed，成本守恒。
- AWS `refresh-cost-attribution-from-summary` 默认会引用 `tcms_cost.resource_allocation`，所以表需要先创建，并确认
  Cost Insight 现有 SQL user 能 `SELECT * FROM tcms_cost.resource_allocation`。
- GCP/pingcap-testing-account 不受影响：GCP query 不产出 `vendor_tags_json`，hash 在 JSON 为 NULL 时沿用旧字段集合。
- summary/unmatched 的 hash：`vendor_tags_json IS NULL` 时按 legacy 字段计算；有 JSON 时纳入 hash。
- 若 BigQuery 后补 tag，普通 upsert 写入前会删除同 legacy 维度下旧的 NULL JSON 行，避免双算。
- 若 tag 被移除（labeled -> unlabeled），不做对称删除，避免误删真实共存的 tagged/untagged 成本；这种历史修正使用
  `sync-aws-billing-summary --replace-existing-partitions` 重抓整个月份 partition。
- `refresh-cost-attribution-from-summary` 会在 join/where 中用 `JSON_EXTRACT/JSON_CONTAINS` 匹配
  `vendor_tags_json`，并用 `ROW_NUMBER` 选择最具体 allocation。当前按 account/date 过滤后离线运行，预计可接受；
  上线后需要观察 AWS refresh 耗时。若成为瓶颈，可在 sync 时把 hot tag 抽成 generated column，并按
  `(usage_date, vendor, account_id, generated_tag)` 建索引。

历史重算建议：

```bash
sync-aws-billing-summary \
  --replace-existing-partitions \
  --export-partition-start <YYYY-MM-01> \
  --export-partition-end <YYYY-MM-01>

refresh-cost-attribution-from-summary \
  --split-by-day \
  --start-date <date> \
  --end-date <date>
```

## 7. 关键文件

| 文件 | 改动 |
| --- | --- |
| `sql/006_add_label_allocation_dimensions.sql` | 新增 `vendor_tags_json/service/project/service_exec_id/allocate_method` 等列 |
| `docs/tcms-resource-allocation-setup.sql` | 手动创建 `tcms_cost.resource_allocation` 和 TCMS user grant |
| `src/cost_insight/sources/aws_billing_export.py` | AWS query 生成 `vendor_tags_json` |
| `src/cost_insight/jobs/sync_gcp_billing_summary.py` | summary 写入支持 `vendor_tags_json` 和 hash 兼容 |
| `src/cost_insight/jobs/sync_gcp_unmatched_resources.py` | unmatched 写入支持 `vendor_tags_json` 和 hash 兼容 |
| `src/cost_insight/jobs/refresh_attribution_daily.py` | AWS summary attribution join tcms facts，并做 shared pool 分摊 |
