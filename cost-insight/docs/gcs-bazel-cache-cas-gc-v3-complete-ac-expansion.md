# GCS Bazel Cache CAS GC v3: 全量 AC→CAS 索引重建

## Context

### 历史演进

**v1 — 独立 LRU 删除（已废弃）**

AC 和 CAS 各自按 `last_seen_at` 独立删除。CAS 被删除时仍有 live AC 引用它，
导致 CI `Missing digest` 错误。

**v2 — 持久化全局 AC→CAS 索引（已废弃但可复用表结构）**

建立持久化的 `gcs_cache_ac_cas_references` 表，256 shard，先 bootstrap 全量 AC
再每日增量同步。Cleanup 时从索引反查 CAS 的 AC 引用关系。

问题：
- Bootstrap 256 shard 逐个跑太慢
- 增量同步额外运维负担
- 当时 AC 量大，投入产出比存疑

**v3 — Per-Run AC 驱动级联（当前版本，将被替换）**

每次 run 选一批冷 AC → 解析提取 CAS 引用 → 删 AC → 删更冷的被引用 CAS。
核心问题：
- **Shared CAS**：一个 CAS 可能同时被"选中的冷 AC"和"未选中的 warm AC"引用，
  v3 不检查后者，靠 `cleanup_max_delete_cas_objects=500` 硬扛
- 每次 run 只覆盖一小批 AC，大量 CAS 永远没机会被评估

### 踩过的坑

1. **Parse error AC 导致引用信息缺失**：protobuf 解析失败的 AC 不贡献 CAS 引用，
   其引用的 CAS 变成"假阴性"。**设计原则：parse error 必须 fail-closed。任一 shard
   有 parse error 就不得更新 `indexed_through`。**

2. **Missing AC 导致引用丢失**：`last_seen_current` 中的 AC 在 GCS 上已不存在
   （NotFound），其引用信息永久丢失。需要将 missing AC 从 `last_seen_current`
   和 references 表中 reconcile 掉。

3. **`ac/one`、`ac/missing` 非法 hex 名称**：`FROM_HEX(SUBSTR(object_name, 4, 2))`
   遇到非 hex 字符直接报错。已确认共 2 行。

4. **Cleanup 自身 GET 污染 audit recheck**：通过 `last_seen_excluded_get_principal_email`
   排除自身 service account 解决。

5. **BigQuery stage table O(n²) 陷阱**：当前 sync 实现每 batch append stage 后立即
   replace 且不清空 stage，重复处理累积数据。3000 万 AC 时不可接受，必须改为
   "先完整收集再一次性 replace"或"每 batch 清空 stage"。

6. **CLUSTER 方向与写入模式冲突**：按 CAS cluster 可以加速反查，但写入是按 AC
   delete + insert，按 CAS cluster 会导致写入扫全表。

7. **SQL reserved word、DDL 分隔符、manifest generation** 等 BigQuery 特有坑。

## Current State

```
AC 总量（last_seen_current）:    ~30,413,613
CAS 总量（last_seen_current）:    ~76,104,392
平均每个 AC 引用 CAS 数:          ~1.84
预估全量引用行数:                 5600w - 8200w 行
无效 hex AC 名称:                 ac/one, ac/missing（共 2 行）
实测 bootstrap 吞吐:              ~28-33 AC/s/pod（3 shard 实测数据）
```

## Goal

重建完整的持久化 `AC → CAS` 引用索引，然后：

1. **CAS 反推 AC 删除**：选出冷 CAS → 反查引用它的所有 AC → 删除其中冷的 AC →
   引用计数归零后删除 CAS。不再需要独立的 AC LRU 删除。
2. **零引用 CAS 删除**：选出没有任何 AC 引用的冷 CAS → 直接删除。

## Design

### 前置步骤 0：AC Source 完整性验证

**问题**：Bootstrap 以 `last_seen_current` 为 AC 来源。如果 GCS 上存在 live AC
但不在 `last_seen_current` 中，它引用的 CAS 会被误判为零引用。

**`last_seen_current` 覆盖范围**：
- 数据来源：`storage.objects.get` + `storage.objects.create` 审计日志
- 日志窗口起始：2026-05-25
- 历史静默对象已在 one-time historical cleanup 中删除（当时删了约 5580 万 AC）

**验证方案**：

1. 获取 GCS inventory 快照，提取所有 AC object names
2. 直接 anti-join：`inventory_ac_names LEFT JOIN last_seen_current ON object_name`
   → 找出在 inventory 中存在但 current 中不存在的 AC
3. 不要用 `last_seen_at <= inventory_snapshot_time` 过滤 current——
   一个在 inventory 快照中存在、快照后被访问过的对象，其 `last_seen_at` 会晚于
   snapshot_time，用时间过滤会错误地把它排除
4. 对不在 current 中的 AC，验证它们在 GCS 上是否仍然 live：
   - Live → current 表有覆盖缺口，需纳入 bootstrap
   - 已删除 → churn，可以忽略
5. 如果 live 缺口 > 1%，bootstrap source 改为 inventory + current UNION

如果差异不可忽略，bootstrap 的 AC 来源需要改为 inventory + current 的 UNION。

**前置结论**：在验证通过之前，不能声称"完整索引"。验证是 Step 0。

### 前置步骤 1：修复 invalid hex name guard + 索引表重建

**代码改动**：

1. 所有使用 `_ac_shard_expression` 的 source query 加 regex guard：
   ```sql
   AND REGEXP_CONTAINS(object_name, r'^ac/[0-9a-fA-F]{64}$')
   ```
   涉及文件：
   - `sync_gcs_cache_ac_references.py`：`build_bootstrap_..._source_query`、
     `build_incremental_..._source_query`
   - `cleanup_gcs_cache.py`：`build_cleanup_gcs_cache_ac_seed_table_query`

2. 手动清理 `last_seen_current` 中 `ac/one`、`ac/missing` 两行。

### 双表设计：解决写入方向和查询方向冲突

**问题**：写入（bootstrap/incremental/AC reconcile）按 `ac_object_name` 做 DELETE + INSERT
replace；查询（cleanup CAS 反查）按 `cas_object_name` 过滤。同一个表只能 CLUSTER
一个方向。

**方案**：`by_ac` 是唯一持续维护的表。`by_cas` 是 cleanup 前从 `by_ac` 一次性
重建的快照表。

```
gcs_cache_ac_cas_refs_by_ac
  shard INT64 NOT NULL
  ac_object_name STRING NOT NULL
  cas_object_name STRING NOT NULL
CLUSTER BY shard, ac_object_name
-- 用途：bootstrap/incremental 写入，AC reconcile
-- 唯一持续维护的表

gcs_cache_ac_cas_refs_by_cas
  ac_object_name STRING NOT NULL
  cas_object_name STRING NOT NULL
CLUSTER BY cas_object_name, ac_object_name
-- 用途：cleanup CAS 反查
-- 只做全量快照重建，不做 per-row 增量维护
```

**为什么 by_cas 不做增量维护**：by_cas 按 `cas_object_name` 聚簇，任何
`WHERE ac_object_name IN (...)` 的 DELETE 都会全表扫描。在 5600w-8200w 行
的大表上，即使"少量行"的按 AC 删除也是不可接受的。

**数据流**：

```
bootstrap/incremental
  → 写入 by_ac（DELETE by ac + INSERT，走 ac cluster，高效）
  → 不立即更新 by_cas

cleanup 执行时：
  → EXPORT by_ac → IMPORT 重建 by_cas（全量快照）
  → 基于 by_cas 做 CAS 反查
  → CAS 删除完成后，by_cas 使命结束（保留到下次 cleanup 被覆盖）
```

```
AC reconcile（cleanup 内 AC 删除后 / 索引保鲜发现幽灵 AC）
  → DELETE from by_ac WHERE ac_object_name IN (...)  -- 走 ac cluster，高效
  → by_cas 不做 per-row 删除——下次 cleanup 重建时会自动消失
```

**表结构**：

```sql
-- 重建（Step 1 执行）
DROP TABLE IF EXISTS `...gcs_cache_ac_cas_refs_by_ac`;
CREATE TABLE `...gcs_cache_ac_cas_refs_by_ac` (
  shard INT64 NOT NULL,
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
)
CLUSTER BY shard, ac_object_name;

DROP TABLE IF EXISTS `...gcs_cache_ac_cas_refs_by_cas`;
CREATE TABLE `...gcs_cache_ac_cas_refs_by_cas` (
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
)
CLUSTER BY cas_object_name, ac_object_name;
```

**索引状态表**：

```sql
-- 重置所有 shard 状态
UPDATE `...gcs_cache_ac_reference_index_state`
SET indexed_through = NULL
WHERE TRUE;
```

**Cleanup gate**：所有 256 shard `indexed_through IS NOT NULL`，沿用 v2。

### Phase 1: 全量 Bootstrap

**关键改造：消除 O(n²) stage table 累积**

当前 sync 实现的 bug：每批数据 append 到 stage table 后立刻跑 replace，但 stage
不清空，导致第 N 批 replace 的 DELETE 扫描前面所有 N-1 批的累积数据。

**修正后的每 shard 流程**：

```
1. 从 last_seen_current 读取该 shard 的所有合法 AC 对象
2. 分批下载 + protobuf 解析（batch_size=500，workers=64）
3. 每批解析结果写入临时 JSONL 文件（本地磁盘），不做 stage table
4. Parse error → 该 shard 失败（不更新 indexed_through）
5. Missing (NotFound) → 收集到 missing stage
6. 所有 batch 处理完后：
   a. DELETE FROM by_ac WHERE shard = @shard; INSERT INTO by_ac SELECT @shard, ... FROM stage
   b. Missing AC → reconcile 出 last_seen_current（DELETE）
   c. Missing AC → reconcile 出 by_ac（DELETE WHERE shard = @shard AND ac_object_name IN missing）
7. parse_error = 0 → 更新 indexed_through
```

**每 shard 写入策略**：`by_ac` 加 `shard INT64` 列，`CLUSTER BY shard, ac_object_name`。
每个 shard 用独立临时 stage 表，完成后用 `DELETE WHERE shard = @shard` + `INSERT` 替换。

```
-- by_ac 表结构（加 shard 列）
CREATE TABLE gcs_cache_ac_cas_refs_by_ac (
  shard INT64 NOT NULL,
  ac_object_name STRING NOT NULL,
  cas_object_name STRING NOT NULL
)
CLUSTER BY shard, ac_object_name;

-- Bootstrap 写入：每行带 shard 值
INSERT INTO by_ac (shard, ac_object_name, cas_object_name)
SELECT @shard, ac_object_name, cas_object_name
FROM _tmp_shard_stage;

-- Shard 替换：DELETE + INSERT
DELETE FROM by_ac WHERE shard = @shard;
INSERT INTO by_ac (shard, ac_object_name, cas_object_name)
SELECT @shard, ac_object_name, cas_object_name
FROM _tmp_shard_stage;
```

`CLUSTER BY shard, ac_object_name` + `WHERE shard = @shard` 是明确的 cluster key
过滤，BigQuery 可以确定性地走 cluster scan，不会有函数表达式阻止优化的问题。

**代码改动**（`sync_gcs_cache_ac_references.py`）：

```python
# 改造前（O(n²)）：每 batch append + replace，stage 不清空
# 改造后：收集全 shard 的数据到本地或独立 stage，一次性 replace

if parse_error_count > 0:
    # 不更新 indexed_through，抛出异常
    raise RuntimeError(
        f"Shard {shard} has {parse_error_count} parse errors, "
        f"fix before retry"
    )

# Missing AC reconcile（新增独立步骤）
# by_cas 不做 per-row 删除——下次 cleanup 重建时自动消失
_delete_from_last_seen_current(missing_ac_names)
_delete_from_references_by_ac(missing_ac_names)
```

**Bootstrap 耗时估算**（基于实测）：

```
实测吞吐:            ~28-33 AC/s/pod（3 shard，92k AC / 46-55min）
单 pod 全量:         30M / 30 AC/s ≈ 278h ≈ 12 天
16 并行 (16 pods):   30M / (30 × 16) ≈ 17-19h
32 并行 (32 pods):   30M / (30 × 32) ≈ 9-10h
```

如果 live AC 比例只有 50%（1500 万实际需要下载），wall-clock 时间减半。

**Bootstrap 完成后**：一次性 EXPORT by_ac → IMPORT 重建 by_cas。

### Phase 2: 增量同步 + 索引保鲜

全量 bootstrap 之后，索引面临三个方向的 drift：

| 变化类型 | 来源 | 增量同步能否感知 | 索引影响 |
|---------|------|:---:|------|
| AC 新建 | `storage.objects.create` | ✅ | 新增引用行 |
| AC 覆盖 | `storage.objects.create`（同名新版本） | ✅ | 替换引用行 |
| AC 删除 | 我们的 cleanup 删除 | ❌ 当前增量同步不消费 delete 事件 | 孤儿引用行堆积 |
| AC 删除 | 外部原因（极少） | ❌ 当前增量同步不消费 delete 事件 | 孤儿引用行堆积 |

关键问题：**增量同步只消费 `storage.objects.create` 事件**。虽然 GCS audit log
中确实有 `storage.objects.delete` 事件，但当前实现不读取它。因此 AC 删除无法通过
增量同步自动清理索引中的孤儿引用行。

这不会导致错误删除 CAS（孤儿引用反而保护了 CAS），但会使索引膨胀、cleanup 效果
递减——越来越多 CAS 被已不存在的 AC "幽灵引用"，无法被识别为零引用。

**方案：Cleanup 内嵌索引保鲜（Stale Reference Reconciliation）**

每次 cleanup 在计算 zero-ref CAS 之前，对索引做一次保鲜：

```
1. 从 by_ac 中找出所有 distinct ac_object_name
2. LEFT JOIN last_seen_current（object_kind='ac'）:
   a. 在 current 中的 AC → 保留（大多数情况）
   b. 不在 current 中的 AC → 候选"幽灵 AC"
3. 对候选幽灵 AC：
   - 批量 GCS HEAD/download 验证是否存在
   - 如果 GCS 上 live → current 表可能 stale，不处理
   - 如果 GCS 上 NotFound → 确认已删除，reconcile：
     * DELETE FROM by_ac WHERE ac_object_name = ...
     * DELETE FROM last_seen_current WHERE object_name = ...（如仍有）
     * by_cas 不做 per-row 删除——下次 cleanup 重建时自动消失
4. 保鲜完成后，再执行 CAS 反查和 zero-ref 计算
```

**开销分析**：
- Step 1-2 是 BigQuery anti-join，对 3000 万 distinct AC，几乎瞬时完成
- Step 3 中"不在 current 中的 AC"通常极少（如果 current 保持每日同步），
  只有被 cleanup 删除的 AC 才可能出现在这里
- GCS 验证只需要 HEAD 请求（或 download 尝试捕获 NotFound），成本很低

**这个设计意味着**：不需要独立的"日常增量同步"job。增量同步和索引保鲜都发生在
cleanup 执行时：
- **增量 catch-up**（Phase 2a）：读取 create 事件，更新索引 ← 处理"增/改"
- **索引保鲜**（Phase 2b）：anti-join + GCS 验证，清理幽灵引用 ← 处理"删"

两者合在一起保证 cleanup 开始时的索引是完整的。

**日常增量频率**：如果 cleanup 每周跑一次，两次 cleanup 之间索引有 7 天的增量
未处理。但 cleanup 前置 catch-up 是强制的——catch-up 会把 7 天的增量补齐后再进入
删除流程，所以**安全性不受影响**。

每周跑增量 vs 每天跑增量的差异只在**延迟和成本**：
- 每天跑：单次处理几千行 create event，几乎零成本；dry-run 随时能看到准确候选量
- 每周跑：一次处理 7 天的 create event（几万行），BQ 扫描稍多；dry-run 在两次
  cleanup 之间看到的候选量可能偏保守（漏掉了新 AC 的引用）

两种方式都不影响 cleanup 的安全性。

**Cleanup 前置强制 catch-up**：

安全论证：

> 新建/覆盖的 AC 可以引用任意既有的 CAS digest（重用之前的构建产物），
> 且这个引用不会产生新的 CAS `get`/`create` audit 事件。
> 因此即使 CAS 年龄 > 16 天，仍可能被一个刚创建的 warm AC 引用。

所以 cleanup 执行前必须：

1. 定义固定 `cleanup_snapshot_time = NOW()`（一次性取值，不作为动态表达式）
2. 跑增量同步 catch-up，覆盖到 `cleanup_snapshot_time`
3. 验证所有 256 shard `indexed_through >= cleanup_snapshot_time`
4. 不满足 → cleanup 拒绝执行

**注意**：`indexed_through >= CURRENT_TIMESTAMP()` 是不可实现的——watermark
总是早于检查时刻。必须用固定 snapshot。

**竞态残余风险**（无法完全消除）：
即使 catch-up 到 `cleanup_snapshot_time`，cleanup 本身需要时间执行（metadata
解析、AC 删除、CAS manifest export + delete）。在 cleanup 执行期间，新的 AC 仍
可能被上传并引用待删除的冷 CAS，且不对 CAS 产生 audit 事件。orphan-first
不能防止这种新引用：snapshot 时的 orphan CAS 可能在运行期间第一次被新 AC 引用。

**缓解措施**：
- 当前实现只做一次全量 `by_cas` rebuild，避免第二次重建大表。
- AC 删除后、CAS manifest export 前再跑一次 incremental catch-up，只把 `by_ac`
  推进到 post-AC-delete 时间点。
- 基于本轮 `ac_removed` 临时表从 snapshot `by_cas` 中逻辑扣减引用，得到
  zero-ref snapshot 候选。
- 随后用当前 live `by_ac` 对 zero-ref snapshot 候选做 blocklist recheck：
  只要仍有任意 AC ref 指向该 CAS，就排除该 CAS。这一步检测 cleanup 运行期间
  新建 AC 对 orphan / linked CAS 的引用，不依赖 `last_seen_current`。
- 这个 live recheck 按 `cas_object_name` 访问 `by_ac`，而 `by_ac` 当前是
  `CLUSTER BY shard, ac_object_name`，因此需要按全表扫描级别评估成本。它省掉的是
  第二次 `by_cas` 全量重建和写入，不是省掉全部 ref 表读取。若成本过高，后续应增加
  `cas_object_name` 访问路径，或给 `by_ac` 增加 `indexed_at` 后改查 post-snapshot delta。
- 2026-07-06 实测：当前 `by_ac` 约 36.7M rows / 5.4GB logical bytes；用
  1M candidate 形状执行 live recheck count，实际 processed/billed 约 2.65GB，
  final execution duration 约 28.9s。当前可接受，但需要随 ref 表增长继续观察。
- CAS cap 和 AC cap 作为最后防线，限制异常情况下的影响半径。

**Implementation**：

```
cleanup():
    snapshot_time = NOW()  # 固定

    # Phase 2a: 增量 catch-up（处理增/改）
    run_incremental_sync(until=snapshot_time)
    assert all_shards_indexed_through >= snapshot_time

    # Phase 2b: 索引保鲜（处理删）——by_ac 现已完整
    run_stale_reconciliation()

    # 重建 by_cas 快照 —— 必须在 catch-up + 保鲜之后
    rebuild_by_cas_from_by_ac()

    # Phase 3: orphan-first CAS selection（受 CAS cap 限制）
    cold_cas = select_cold_cas_orphan_first(snapshot_time, limit=CAS_CAP)
    acs_to_delete = reverse_lookup_ac(cold_cas, age > ac_cutoff, limit=AC_CAP)
    delete_ac(acs_to_delete)
    ac_removed = reconcile_ac(acs_to_delete)  # deleted + confirmed missing AC

    # 基于初始 by_cas snapshot 逻辑扣减本轮移除的 AC，避免第二次 by_cas rebuild
    zero_ref_snapshot = recompute_zero_ref_after_ac_removal(cold_cas, ac_removed)

    # Phase 4: post-delete catch-up + live by_ac recheck
    post_delete_recheck_time = NOW()
    run_incremental_sync(until=post_delete_recheck_time)
    assert all_shards_indexed_through >= post_delete_recheck_time
    cas_to_delete = exclude_cas_with_any_live_by_ac_ref(zero_ref_snapshot)

    # CAS delete phase（受 CAS cap 限制）
    delete_cas(cas_to_delete)
    reconcile_cas(cas_to_delete)
```

### Phase 3: CAS 反推 AC 清理

Cleanup 查询逻辑（分为两个阶段）：

### 查询 A：AC 删除规划（by_cas 第一次重建后执行）

```sql
-- A1: 两阶段 CAS 选择（解决 bytes cap 不可执行问题）
-- Phase A — 按 last_seen_at 初筛（只取 object name，数量 = 预设上限）
CREATE TEMP TABLE cold_cas_preselect AS
SELECT object_name, last_seen_at
FROM last_seen_current
WHERE object_kind = 'cas'
  AND last_seen_at < TIMESTAMP_SUB(@snapshot_time, INTERVAL @cas_cutoff_days DAY)
ORDER BY last_seen_at ASC
LIMIT @cas_preselect_limit;  -- e.g., 2× CAS object cap

-- Phase B — Metadata stage（只对初筛结果做 GCS HEAD）
-- → 获取 generation + size_bytes
-- → Missing → reconcile 出 last_seen_current

-- Phase C — 按 size 二次筛选，应用 bytes cap
CREATE TEMP TABLE cold_cas AS
SELECT object_name, last_seen_at
FROM cold_cas_with_metadata
ORDER BY size_bytes DESC
LIMIT @cas_object_cap;  -- 最旧 N 个中取最大的，双重约束

-- A2: 反查引用关系 → 规划 AC 删除列表
CREATE TEMP TABLE ac_to_delete AS
SELECT DISTINCT
  refs.ac_object_name,
  cur.last_seen_at
FROM gcs_cache_ac_cas_refs_by_cas AS refs
JOIN cold_cas
  ON refs.cas_object_name = cold_cas.object_name
JOIN last_seen_current AS cur
  ON cur.object_name = refs.ac_object_name
 AND cur.object_kind = 'ac'
 AND cur.last_seen_at < TIMESTAMP_SUB(@snapshot_time, INTERVAL @ac_cutoff_days DAY)
ORDER BY cur.last_seen_at ASC
LIMIT @ac_object_cap;  -- AC cap

-- → 执行 AC 删除 + reconcile（只删 by_ac）
```

### 查询 B：zero-ref snapshot 判断（AC 删除后，基于本轮移除集逻辑扣减）

```sql
-- 不做第二次 by_cas 重建。用本轮成功删除或确认 missing 的 AC 集合扣减
-- cleanup snapshot 中的 refs。
CREATE TEMP TABLE zero_ref_snapshot AS
SELECT cold_cas.object_name, cold_cas.last_seen_at
FROM cold_cas
LEFT JOIN gcs_cache_ac_cas_refs_by_cas AS refs
  ON refs.cas_object_name = cold_cas.object_name
LEFT JOIN ac_removed
  ON ac_removed.object_name = refs.ac_object_name
GROUP BY cold_cas.object_name, cold_cas.last_seen_at
HAVING COUNTIF(
  refs.ac_object_name IS NOT NULL
  AND ac_removed.object_name IS NULL
) = 0;
```

### 查询 C：live `by_ac` 新引用 recheck（CAS manifest export 前执行）

```sql
-- AC 删除成功后，先再跑一次 incremental sync，把 by_ac 推进到当前时间点。
-- 然后只检查本轮 zero-ref snapshot 候选是否仍有 live by_ac ref。
CREATE TEMP TABLE blocked_by_live_ac_ref AS
SELECT DISTINCT cas.object_name
FROM zero_ref_snapshot AS cas
JOIN gcs_cache_ac_cas_refs_by_ac AS refs
  ON refs.cas_object_name = cas.object_name;

CREATE TEMP TABLE cas_to_delete AS
SELECT source.object_name, source.last_seen_at
FROM zero_ref_snapshot AS source
LEFT JOIN blocked_by_live_ac_ref AS blocked
  ON blocked.object_name = source.object_name
WHERE blocked.object_name IS NULL;
```

所有中间结果保留为 row-level，不做 ARRAY_AGG。

### Phase 4: 零引用 CAS 清理

```sql
-- 零引用 CAS（在 by_cas 中完全没有记录的冷 CAS）
-- 用 NOT EXISTS 替代 NOT IN，避免 BigQuery NULL 语义坑和优化器差异
SELECT cas.object_name, cas.last_seen_at
FROM last_seen_current AS cas
WHERE cas.object_kind = 'cas'
  AND cas.last_seen_at < TIMESTAMP_SUB(@snapshot_time, INTERVAL @cas_cutoff_days DAY)
  AND NOT EXISTS (
    SELECT 1
    FROM gcs_cache_ac_cas_refs_by_cas AS refs
    WHERE refs.cas_object_name = cas.object_name
  )
```

这些 CAS 直接进入安全检查 + delete 流程，不需要前置 AC 删除步骤。

### CAS 删除前安全检查

```
对每个 CAS delete candidate：
  1. 解析 live GCS metadata（generation, size）
     - Missing → reconcile 出 last_seen_current
  2. 检查 size_bytes，累计 bytes 用于 cap 判断
  3. Raw audit recheck（无近期 get/create，排除自身 service account）
  4. 按 last_seen_at ASC + bytes DESC 排序
  5. 应用 CAS cap：MIN(object_count_limit, bytes_limit)
  6. EXPORT manifest（bucket, name, generation）→ Storage Batch Operations delete
  7. Reconcile 已删除 CAS
```

### 清理流程图

```
                    ┌─────────────────────────────┐
                    │ 固定 cleanup_snapshot_time    │
                    │ Phase 2a: 增量 catch-up       │
                    │ 验证：256 shard 均 >= snapshot │
                    └────────────┬────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────────┐
                    │ Phase 2b: 索引保鲜            │
                    │ anti-join current → 候选幽灵  │
                    │ GCS 验证 → reconcile 孤儿引用  │
                    └────────────┬────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────────┐
                    │ ★ 重建 by_cas（第 1 次）      │
                    │ EXPORT by_ac → IMPORT by_cas │
                    └────────────┬────────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │ last_seen_current         │
                    │ (bounded cold CAS)        │
                    │ [受 CAS cap 约束]          │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │ gcs_cache_ac_cas_refs_    │
                    │ by_cas                    │
                    │ (CLUSTER BY cas_name)     │
                    │                          │
                    │ CAS ──反查──→ ACs         │
                    │ (row-level, 无 ARRAY_AGG) │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
          ┌─────────────────┐      ┌─────────────────┐
          │ AC 有引用        │      │ AC 零引用        │
          │ (cold CAS 被     │      │ (CAS 不在        │
          │  至少一个 AC 引用) │      │  by_cas 中)      │
          └────────┬────────┘      └────────┬────────┘
                   │                        │
                   ▼                        │
          ┌─────────────────┐               │
          │ 选冷 AC 删除      │               │
          │ (age > ac_cutoff)│               │
          │ reconcile from   │               │
          │ last_seen + by_ac│               │
          └────────┬────────┘               │
                   │                        │
                   ▼                        │
          ┌─────────────────┐               │
          │ ac_removed 逻辑扣减              │
          │ snapshot by_cas refs             │
          └────────┬────────┘               │
                   │                        │
                   ▼                        ▼
          ┌─────────────────────────────────┐
          │ zero-ref snapshot               │
          └───────────────┬─────────────────┘
                          │
                          ▼
          ┌─────────────────────────────────┐
          │ post catch-up + live by_ac       │
          │ ref blocklist                    │
          └───────────────┬─────────────────┘
                          │
                          ▼
          ┌─────────────────────────────────┐
          │ 最终 CAS delete candidates       │
          │ + live metadata (generation,     │
          │   size_bytes) check             │
          │ + audit recheck                 │
          └────────────┬────────────────────┘
                       │
                       ▼
          ┌─────────────────────────────────┐
          │ [受 CAS object + bytes cap 限制] │
          │ Storage Batch Operations Delete │
          │ + reconcile                     │
          └─────────────────────────────────┘
```

## AC 删除半径控制

CAS-driven AC 删除有两个独立的风险维度：

1. **cold_cas 候选集巨大**：即使只取年龄 > 16 天的 CAS，可能仍有千万级候选
2. **热门 CAS 被大量 AC 引用**：一个 CAS 可以被几百个冷 AC 引用。10k CAS × 100 AC
   = 百万级 AC 删除

### 两阶段 CAS 选择（解决 bytes cap 不可执行问题）

CAS bytes cap 面临一个鸡生蛋问题：`last_seen_current` 没有 `size_bytes` 字段，
size 要到 metadata stage（GCS HEAD）才有。但 HEAD 4500 万 cold CAS 只是为了
排序是不可接受的。

**方案**：两阶段选择，HEAD 规模受控。

```
Phase A: 按 last_seen_at 初筛
  SELECT object_name
  FROM last_seen_current
  WHERE object_kind = 'cas'
    AND last_seen_at < cutoff
  ORDER BY last_seen_at ASC
  LIMIT @cas_preselect_limit   -- e.g., 2× CAS object cap = 20000

Phase B: Metadata stage（只 HEAD 这 20000 个 CAS）
  → 获取 generation + size_bytes
  → Missing → reconcile 出 last_seen_current

Phase C: 按 size 二次筛选
  → ORDER BY size_bytes DESC
  → 应用 bytes cap 截断
  → 最终 bounded cold_cas（如 ~8000 个，符合 object + bytes cap）
```

这个两阶段选择既实现了"优先删大文件"的优化目标，又限制了 HEAD 请求数量。

### AC cap

即使 cold_cas 被 bounded 到 10k，每个 CAS 可能被数十到上百个冷 AC 引用，
反推出的 AC 仍可达百万级。**需要一个独立的 AC cap**——便宜，省事故。

```
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_AC_OBJECTS=100000  # rollout 期
```

AC 删除候选超过 cap 时：按 `last_seen_at ASC` 截断（删最旧的 AC 优先）。
Summary 输出 `candidate_ac_delete_count`（全量候选数）和 `selected_ac_delete_count`
（经 AC cap 截断后实际删除数）。

**多轮语义**：AC cap 截断后，被截掉的 AC 引用的 CAS 本轮不会归零，不会被删。
这是预期行为——下轮 cleanup 时这些 AC 更冷了（或已被其他轮次删除），CAS 终将
在多轮后归零。Summary 的 `candidate_ac_delete_count - selected_ac_delete_count`
让 operator 看到还有多少 AC 在排队。

## CAS Cap：object count + bytes 双重限制

仅 object count cap 不够——10000 个小 CAS 和 10000 个大 CAS 的 blast radius
差异可以有几个数量级。

```
# Object count cap（rollout 期）
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_CAS_OBJECTS=10000

# Bytes cap（新增）
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_CAS_BYTES=54975581388800  # 50 TiB
```

**CAS metadata stage 新增 size 字段**：

```
cas_live_metadata:
  object_name STRING
  generation INT64
  size_bytes INT64    -- 新增
```

**排序策略**：`ORDER BY last_seen_at ASC, size_bytes DESC`——先删最旧且最大的，
在 preselect 窗口内优先大对象（非全局最优，但限制了 HEAD 成本）。

**Summary 新增字段**：
- `candidate_cas_zero_ref_count`：零引用 CAS 候选总量（dry-run / 实际 run 均输出）
- `candidate_cas_zero_ref_bytes`：零引用 CAS 候选总 bytes
- `planned_cas_delete_bytes`：本 run 计划删除的 bytes
- `deleted_cas_bytes`：实际删除的 bytes

**Dry-run 语义**：dry-run 不执行 post-delete catch-up，也不做 live `by_ac`
recheck，因此 CAS delete candidate count 是执行前基于 snapshot 的上界估算。真实
delete 可能因为 post-delete live ref blocklist 再排除一部分 CAS。

## Implementation Plan

### Step 0: AC Source 完整性验证

- [ ] 获取最新 GCS inventory 中 live AC 数量
- [ ] 对比 `last_seen_current` 中 AC 数量
- [ ] 如果差异显著，修正 bootstrap source（inventory + current UNION）

### Step 1: 修 invalid hex name guard + 重建双表

- [ ] 所有 AC source query 加 `REGEXP_CONTAINS(object_name, r'^ac/[0-9a-fA-F]{64}$')`
- [ ] 手动清理 `last_seen_current` 中 `ac/one`、`ac/missing`
- [ ] DROP + CREATE `gcs_cache_ac_cas_refs_by_ac`（加 `shard INT64` 列，CLUSTER BY shard, ac_object_name）
- [ ] DROP + CREATE `gcs_cache_ac_cas_refs_by_cas`（CLUSTER BY cas_object_name）
- [ ] `UPDATE ... SET indexed_through = NULL WHERE TRUE`（256 行全部重置）

### Step 2: Fix O(n²) stage table + parse error fail-closed

- [ ] `sync_gcs_cache_ac_references.py`：
  - 改为"收集全 shard 数据 → 一次性 replace"模式，消除 O(n²)
  - parse_error_count > 0 → 不更新 `indexed_through`，抛异常
  - Missing AC → 生成 missing stage → 执行 reconcile（从 last_seen_current + by_ac 删除；by_cas 下次重建时自动消失）
- [ ] Cleanup readiness gate：所有 256 shard `indexed_through IS NOT NULL`

### Step 3: 全量 Bootstrap

- [ ] 跑全量 bootstrap（256 shard，N pod 并行）
- [ ] 监控 parse error，有就修 bug → 重跑对应 shard
- [ ] 验证所有 256 shard `indexed_through` 非 NULL
- [ ] 一次性 EXPORT by_ac → IMPORT 重建 by_cas
- [ ] 记录实测指标：耗时、live AC 比例、引用行数、missing AC 数

### Step 4: 索引保鲜（Stale Reference Reconciliation）

- [ ] 实现 anti-join 查询：找出 by_ac 中不在 `last_seen_current` 的候选幽灵 AC
- [ ] 对候选幽灵 AC 做 GCS HEAD/download 验证
- [ ] NotFound → reconcile（DELETE from by_ac + last_seen_current；by_cas 下次重建时自动消失）
- [ ] 保鲜完成后才进入 CAS 反查

### Step 5: Cleanup 前置增量 catch-up + by_cas 重建 + 单 snapshot 机制

- [ ] 实现固定 `cleanup_snapshot_time`（一次性取值）
- [ ] Cleanup 入口：先增量 catch-up 覆盖到 snapshot_time
- [ ] 验证 gate：所有 shard `indexed_through >= snapshot_time`
- [ ] 再跑索引保鲜（Step 4）
- [ ] **by_cas 重建**：EXPORT by_ac → IMPORT 重建 by_cas
- [ ] orphan-first bounded cold_cas 选择（受 CAS cap 约束，控制 AC 删除半径）
- [ ] AC 删除
- [ ] 用 `ac_removed` 逻辑扣减 snapshot refs 后重新计算 zero-ref snapshot
- [ ] CAS manifest export 前再跑 incremental catch-up，并用 live `by_ac` 排除仍有引用的 CAS

### Step 6: 实现 CAS 反推 AC 清理

- [ ] 新增 `execute_kind: cas-from-index`
- [ ] 实现 orphan-first 两阶段 CAS 选择（初筛 object count → metadata HEAD → bytes cap 二次筛选）
- [ ] 实现 AC 删除规划 query（查询 A）+ AC cap 截断
- [ ] AC 删除后基于 `ac_removed` 做逻辑 zero-ref 查询（查询 B，LEFT JOIN + COUNTIF）
- [ ] post catch-up 后基于 live `by_ac` 做新引用 blocklist（查询 C）
- [ ] 实现新 manifest export 和 delete 流程
- [ ] Dry-run 输出完整中间结果（含 bytes、AC 候选数）

### Step 7: Canary + 灰度

- [ ] CAS cap：object_count=10000, bytes=1TB（或其他合理 rollout 值）
- [ ] 小批量 canary delete
- [ ] 观察 CI `Missing digest` + cache miss 率
- [ ] 逐步提升双重 cap

### Step 8: 清理与下线

- [ ] 移除 v3 per-run AC 展开逻辑（`execute_kind=cas` 旧路径）
- [ ] 设置 cron：
  - Cleanup（每周，含增量 catch-up + 索引保鲜 + CAS 反查删除）
  - 可选：轻量增量同步（每天，只做 create event，不做保鲜）。非必须，
    但可以保持索引热度，让 dry-run 更快看到准确候选量

## 配置变更

新增：
```
# cleanup 是否强制前置增量 catch-up
COST_INSIGHT_GCS_CACHE_CLEANUP_REQUIRE_FRESH_INDEX=true

# CAS bytes cap
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_CAS_BYTES=54975581388800

# CAS 初筛上限（控制 metadata HEAD 数量，建议 2× CAS object cap）
COST_INSIGHT_GCS_CACHE_CLEANUP_CAS_PRESELECT_LIMIT=20000

# AC object cap（新增）
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_AC_OBJECTS=100000
```

修改（保留但调值）：
```
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_CAS_OBJECTS=10000  # rollout 值
```

保留：
```
COST_INSIGHT_GCS_CACHE_AC_RETENTION_DAYS=10
COST_INSIGHT_GCS_CACHE_CAS_RETENTION_DAYS=15
COST_INSIGHT_GCS_CACHE_SAFETY_BUFFER_DAYS=1
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_OBJECTS=10000000
```

## Risk Assessment

### 已消除的风险

- **Shared CAS**：全量索引 → 精确知道每个 CAS 的 AC 引用
- **Parse error 容忍**：fail-closed，shard 不 ready 则 cleanup 不可用
- **Index staleness 时间窗口**：cleanup 强制前置增量 catch-up + 单 snapshot
- **O(n²) stage table**：改为全 shard 收集后一次性 replace
- **CLUSTER 方向与写入冲突**：by_ac 持续维护，by_cas 只做全量快照重建，不增量维护
- **ARRAY_AGG row size**：改为 row-level 输出

### 剩余风险

1. **`last_seen_current` 未覆盖所有 GCS live AC**：
   - 缓解：Step 0 验证。差异存在则改为 inventory + current UNION
   - 严重性：漏掉的 AC 引用的 CAS 会被误删

2. **Missing AC 引用永久丢失**：
   - 缓解：从 last_seen_current + both refs tables reconcile；CAS 受年龄窗口保护

3. **Cleanup 执行期间的新 AC 引用竞态**：
   - 风险：snapshot 时 orphan 或 zero-ref 的 CAS，可能在 cleanup 运行期间被新 AC 第一次引用
   - 缓解：AC 删除后、CAS manifest export 前再次 incremental catch-up，并用 live `by_ac` blocklist 排除仍有引用的 CAS
   - 剩余窗口：post catch-up 完成后到 CAS batch delete 生效前；CAS cap / AC cap 只限制影响半径，不提供逻辑保护

4. **首次零引用 CAS 规模未知**：
   - 缓解：dry-run 先评估规模；双重 cap (object + bytes) 限制

5. **并发 cleanup 运行覆盖 persistent `by_cas` snapshot**：
   - 风险：`by_cas` 是持久表，每次 delete 入口会 rebuild；两个 cleanup run 并发时，后一个 run 可能覆盖前一个 run 仍在使用的 snapshot
   - 当前部署：`cost-insight-cleanup-gcs-cache-delete-cas` CronJob 使用 `concurrencyPolicy: Forbid`，按单实例运行
   - 后续：如果引入手动并发或多个调度入口，需要增加 lease/lock，或改为每 run 独立 snapshot 表
