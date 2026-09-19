# 腾讯云 CI 非超级节点成本临时分摊设计

状态：Proposed

范围：Tencent Cloud 组织账单账号 `100050658403`。本设计是过渡方案：当腾讯超级节点账单稳定提供可对账的 Pod 级 `author/org/repo` 账单标签后，停止此分摊器，改由该原始账单直接归属。

## 已确认决策

1. 该账号内**所有非超级节点** Tencent L3 成本都进入共享池，包括普通 TKE 节点、CBS/临时存储、COS bucket、网络以及其他普通资源。原始账单已有 `author` 或 `owner` 标签不是豁免条件。
2. 所有超级节点成本都不进共享池，而是保留为 direct attribution。分类只能依据 Tencent L3 的稳定代码字段，绝不依据 SKU/资源显示名称或名称前缀。
3. 分摊粒度是 Tencent 账单 `usage_date`（北京自然日）。V1 的每个已完成 Tencent 构建权重为 `1 + duration_hours`；无有效时长的已完成构建仍贡献 `1`。
4. 资源规格只属于可选 V2。它每周从可验证的 CI job spec 形成不可变 profile snapshot，提供静态 CPU/memory 系数；V1 不读取 Pod 实测值、Pod 生命周期数据或不可靠的 YAML 推断。
5. 原始账单导入、分摊计算和发布是三个独立步骤。导入只更新原始账本并把受影响的 usage date 标为 stale；任意日期范围都可用新的 allocation/weight/profile version shadow 重算，而无需重跑导入。
6. Dashboard 继续读取已发布的 `cost_attribution_daily` 投影，不在读路径计算权重。版本化 staging/history、按日原子发布和回滚保护已发布版本。
7. 未匹配 author 的构建权重形成显式 `(no owner)` residual，绝不转嫁给其他员工。每个 usage date、币种和可用金额字段严格守恒；D+5 更正及任一作业失败均不得破坏已发布版本。

“direct attribution”只表示不经过本设计的权重重分摊；它不保证一定能匹配 roster。没有可匹配标签的超级节点行仍是 direct 且 `unattributed`，不会因此进入共享池。

## 术语与边界

| 术语 | 定义 |
| --- | --- |
| **Tencent 账本** | `cost_bq_export_summary_daily` 中 `vendor='tencent'` 且 `account_id='100050658403'` 的 Tencent L3 component facts；它是金额唯一事实来源。 |
| **超级节点** | 由已审核 Tencent 稳定代码元组判定的 L3 component，而非 `sku_name`、`resource_name` 或标签。 |
| **非超级节点** | 被当前分类版本明确判为 `non_supernode` 的账本行，不是“未命中超级节点规则”的默认值。 |
| **腾讯构建** | `ci_l1_builds` 中 `cloud_phase='TENCENT'` 且 `completion_time` 非空的构建。成功、失败、终止、重试都计入；不因 `state`、author、repo 或时长缺失而排除。 |
| **构建 author** | `ci_l1_builds.author` 记录的 CI author。它用于成本归属，不在 V1 中推断“实际点击 retest 的人”。上线前必须抽样确认产品文案应称“CI-recorded author”还是“PR author”。 |
| **当前团队** | allocation version 创建时从活动 roster 取得的 employee/group/manager，而不是 usage date 当时的历史组织结构。该 roster snapshot 会被版本化保存。 |
| **published projection** | 某一 usage date 当前 Tencent publication pointer 选中的完整输出（超级节点 direct + 非超级节点共享 + residual）写入 `cost_attribution_daily` 的结果。它是 Tencent 的终态 native projection，不是通用派生分摊的 Stage-0 输入。 |

账号边界是唯一的资源边界；不按 TKE cluster、账单标签、资源名、service、项目或已有 owner 先过滤。`service_name` 和 `cost_driver_key` 仅用于保留共享成本的展示/审计池，不改变参与资格或构建权重。

## 数据血缘与读模型

```text
Tencent L3 API
  -> Tencent raw summary ledger
  -> stable-code classification
  -> stale usage-date marker ────────────────────────┐
                                                        │
ci_l1_builds ─┐                                        │
current roster -> immutable roster snapshot             ├-> versioned allocation staging/history
weekly V2 job profiles -> immutable profile snapshot ──┘             │
                                                                      │ validate
                                                                      v
                                                     per-day atomic publication
                                                                      │
                                                                      v
                                                    cost_attribution_daily
                                                                      │
                                                                  Dashboard
```

- 原始账本保留每条 L3 component 的 source identity、金额、账单标签和 Tencent 代码。它不被分摊版本覆盖，也不保存分摊输出。
- 每个 allocation version 保存其 input manifest、每日输入 fingerprint、权重摘要和输出。共享输出引用 `source_pool_key`；超级节点 direct 输出保留实际 `source_summary_row_hash`。共享行的 `source_summary_row_hash` 必须为 `NULL`，不得伪装成单条账单血缘。
- `cost_attribution_daily` 是已发布 serving projection。对这个 Tencent 账号，通用的 “summary → direct attribution” 刷新路径在本方案启用后不得写入该投影；Tencent publisher 是其唯一写入者。
- 非 Tencent source 沿用现有流程。Dashboard API、Dashboard 查询和 CI Dashboard schema 均不在本设计的实现范围内。

### 与既有通用分摊的隔离和写入 gate

既有 `materialize-cost-allocations` 将 `cost_attribution_daily` 视为 Stage-0 native facts，以全局 `cost_allocation_publication` 发布三种派生 perspective；它不能把本方案的已分摊 Tencent projection 当作输入。启用前必须在 `cost_sources` 为此唯一账号持久化不可变的 source policy：

```text
attribution_write_mode = tencent_ci_published_terminal
```

该 policy 的实施契约如下，缺一不可：

1. 通用 `refresh-cost-attribution-from-summary` 的无参数 source discovery 只选择 `direct_summary` source，因而跳过此 Tencent source；显式指定 `--vendor tencent --account-id 100050658403` 也必须在取得写事务、删除行、更新 job state 或失效任何 publication **之前**失败，并提示改用 `materialize-tencent-ci-cost-allocation`。其 library entry point 同样防御性拒绝，不能只依赖 CLI filter。
2. 通用 `materialize-cost-allocations` 在计算 `MAX(usage_date)`、发现 source window、Stage-0 participant denominator、staging 和 `--publish-only` 完整性检查中均排除 `tencent_ci_published_terminal` source。Tencent 的按日发布绝不删除或更新全局 `cost_allocation_publication`；这样单日 Tencent 重发不会强制全历史派生 rebuild，也不会发生 Kubernetes/EQ 的二次分摊。
3. `materialize-resource-serving` 可以把已发布 Tencent projection 当作 native serving input；它不是分摊器，失败只使该派生 serving window stale，绝不回写 Tencent ledger、projection 或 Tencent pointer。

因此 Tencent 在不改 Dashboard 的前提下仍由已发布 `cost_attribution_daily` 提供 native 读模型；通用派生 perspective 对此 source 不可用而不是悄悄遗漏或重分摊它。首次启用必须先部署上述 gate，并证明该账号不存在通用 refresh 写入的 projection，才允许把 source 设为 active。

这覆盖了 Tencent billing import design 中“完成账单分区后刷新 attribution”的 Tencent 下游部分：本方案启用后，完成导入只标记 stale，由独立 allocation/publish 工作流处理；不改变该导入设计的分页、identity、D+5 和月结账本规则。

## 超级节点分类与 direct 输出

### 分类契约

导入器为每个 Tencent L3 component 保留以下原始代码及分类审计信息：

```text
BusinessCode, ProductCode, ComponentCode, ItemCode,
tencent_cost_class, tencent_classification_version
```

分类规则存放在 Cost Insight 的只追加 `tencent_cost_classification_rule_set` artefact：每个已发布 version 保存完整代码元组规则、审阅者/发布时间和 canonical content hash。它不是散落在运行镜像或未版本化配置中的 if/else。显示名称（包括 `BusinessCodeName`、`ProductCodeName`、component/item 名称）、`sku_name`、`resource_name`、Tag 和字符串前缀都不在规则输入中。rule-set version/hash 是 allocation input manifest 的一部分，因此历史版本可复核当时采用的分类。

分类只有三个值：

```text
supernode | non_supernode | unclassified
```

不得用 `tencent_is_supernode DEFAULT 0` 或“规则未命中即非超级节点”。导入仍可持久化 `unclassified` 原始行，但 allocation 必须拒绝发布包含它的 usage date，并报告其代码元组、金额和行数。这样 Tencent 新增超级节点代码时不会被静默分入普通资源。

`publish-tencent-cost-classification` 是分类生命周期的 control-plane job，也是唯一可发布 rule set 的入口；它不调用 Tencent API、不 materialize、也不发布 projection。它先持久化并 seal 新 rule-set artefact，随后在事务中用账本已保存的四个代码字段重新判定所有**完整 source partition**中结果会改变的行，写入新的 class/version，并以变更前后 source fingerprint 标记恰受影响的 usage date stale。partial/in-flight partition 不可重新分类。无行变化的 rule-set 发布不制造 stale。之后运营者以新 classification version 独立 shadow/materialize；导入器只对新/更正的原始行应用当前已发布 rule set。

上线门槛是在真实连续 7 天 L3 账单上确认代码集合，并与 Tencent 控制台的超级节点金额核对：

- 所有已知超级节点行命中 `supernode`；
- `non_supernode + supernode` 覆盖全部可发布 L3 金额；
- 不存在 `unclassified`；
- 普通 TKE、存储、COS、网络等代表性行位于 `non_supernode`。

这是一项分类覆盖门槛，而非允许按 SKU 名称临时兜底的许可。

### 两类输出

对一个可发布 usage date，allocation materializer 生成完整 Tencent 投影：

| 原始类别 | 输出 | 标签处理 |
| --- | --- | --- |
| `supernode` | 一条对应一条 source summary fact 的 `tencent_l3_direct` 行；保留 source hash、全部可用服务/资源维度和原始账单标签。 | 按下面定义的 Tencent snapshot identity resolver 解析 direct author/owner；不进入权重、分母或 residual。 |
| `non_supernode` | 按构建权重生成 `tencent_ci_build_weighted` matched 行和必要的 `(no owner)` residual 行。 | 原始账单的 `author`/`owner`/repo 标签不参与 owner 选择、权重或豁免判断。构建 author 使用相同 Tencent snapshot identity resolver。 |
| `unclassified` | 无可发布输出。 | 阻断该 usage date 的发布，保留当前已发布版本。 |

因此“超级节点 direct”不是把超级节点金额从投影中删掉，而是将其完整、未重分摊地包含在同一日 Tencent published projection 中。

## 时间、构建和 roster 语义

### 日边界与构建快照

Tencent `usage_date` 是 `DATE(FeeBeginTime)` 的北京账单日。`ci_l1_builds` 当前把有时区的 Prow 时间规范化为无时区 UTC；materializer 必须先按 UTC 解释 `start_time`，再转换到 `Asia/Shanghai`，以其本地日期归属构建。构建不跨日切分，`completion_time` 只决定是否完成。

一个构建由稳定的 `source_prow_job_id` 标识。每次 materialize 为每个 usage date 固化一个 build-input fingerprint，覆盖至少：构建 ID、start/completion time、cloud phase、author、org/repo、job name、`run_seconds`、`total_seconds` 和参与资格。已发布版本之后若这些输入变化，新的版本不能伪称复现旧版本；它必须有新的 input fingerprint，并使该日期保持/标记 stale，直到新版本验证发布。

`refresh-tencent-ci-build-staleness` 是必需的只读 detector：每天对每个已有 Tencent ledger 或 publication 的北京 usage date 按上述 canonical identity 排序重算 fingerprint，并与 active Tencent pointer/最新可用 manifest 保存的 fingerprint 比较。差异在独立小事务中标记该日 stale，不写账本、staging、projection 或 pointer。它不以“近期构建稳定窗口”猜测数据稳定性；扫描用一次按日聚合/流式 hash，而不是对每个 pool 重扫 `ci_l1_builds`。materializer 的 staged 和 publish 前比较是第二道保护，不能替代该 detector。

### V1 权重

对每个完成的 Tencent 构建：

```text
duration_seconds = run_seconds   if run_seconds > 0
                 = total_seconds if total_seconds > 0
                 = 0             otherwise

base_weight = 1 + duration_seconds / 3600
```

`1` 是构建次数权重。缺失、零或负时长不会丢弃已完成构建，而是仅贡献该 `1`；负值不得变成负权重。V1 的 active weight 是 `base_weight`，不读取 job YAML、Pod request/limit、Pod 生命周期、CPU/memory 使用率或 node telemetry。

### Tencent snapshot identity resolver

超级节点 direct 和共享构建使用同一份 allocation-version 固化的 Tencent roster identity snapshot，而不是一个沿用 legacy direct fallback、另一个只做 GitHub exact match。该 resolver 只匹配 active employee 及其 active group，按如下固定优先级消费一个不为空的 identity：

1. 已审核、版本化的 Tencent alias map（只用于明确的历史服务 identity，例如现有 `flaky-claw`/`ti-chi-bot` mapping）；
2. case-insensitive `github_id` exact match；
3. case-insensitive email exact match，或唯一的规范化 `github_id`/email local-part/`en_name` match。

每一步多义即停止为 unmatched，不能任选一人；snapshot 外的 alias、inactive employee/group、空 identity、外部用户和未知机器人同样 unmatched。超级节点按现有语义优先解析 direct `owner`，否则 `author`；共享行把 `ci_l1_builds.author` 作为 author identity 送入同一个 resolver。原始 direct 标签和 CI-recorded author 都原样保存，以便区分匹配输入和已解析 owner。

因此同一身份在同一 version 中不会因成本类别不同而得到不同 roster owner。已匹配构建保留其 repo（repo 缺失时为 `NULL`，而不是排除该构建）；unmatched 构建仍计入总构建权重。

### roster snapshot

创建 allocation version 时，materializer 在一个一致读中生成不可变 roster snapshot，含匹配所需的活动 GitHub identity 与显示所需的 `employee_id`、email/en_name、`group_id`、`manager_id`。snapshot 有不可变 ID、内容 hash 和 `resolved_at`：

- 相同 allocation version 重跑必须引用相同 roster snapshot；输入不一致即失败，而不是悄悄改写 staging 或已发布输出。
- 新 allocation version 默认捕获当时的 current roster。因此组织调整后，历史日期可以 shadow 重算，并在批准后以新的“当前团队”发布。
- roster 同步的纯技术时间戳变化不是语义变化；identity、活动状态或团队字段变化才需要新的 snapshot/version。

## 可选 V2：静态资源规格系数

V2 不属于 V1 上线前提，也不创建 V1 对 Pod 数据的依赖。每周（以 `Asia/Shanghai` 周一 00:00 起始）从固定的 CI job-spec repository revision 生成不可变 profile snapshot：

```text
profile_version, week_start, job_name, source_ref, source_hash,
requested_cpu_cores, requested_memory_gib,
memory_gib_per_cpu_reference, parser_version, captured_at
```

只有能从该 revision 中**明确、可重复地解析**的 job spec 才有 profile。V2 只使用显式 container `requests`：app containers 求和、init containers 取最大值；不会由 `limits`、默认值、模板猜测、动态 Jenkins 参数、fan-out 数量或观测到的 Pod 来补全。无法可靠绑定 `job_name` 或无法确定 requests 时，factor 为 `1` 并计入 profile coverage 的未覆盖部分。

对一个有 profile 的构建：

```text
resource_factor = max(
  1,
  requested_cpu_cores,
  requested_memory_gib / memory_gib_per_cpu_reference
)
final_weight = base_weight × resource_factor
```

`memory_gib_per_cpu_reference` 是 snapshot 中版本化的普通 TKE node-pool 静态规格基准；不是运行时 Pod/节点实测值。构建使用其 `start_time` 所属周的 snapshot，而非“今天最新”的 profile。一个 V2 allocation manifest 必须固定完整的 weekly snapshot set 和 profile coverage；历史 shadow 重跑仍引用该 set。

V2 首先只以 `--no-publish` shadow 同一日期范围，报告 V1/V2 Owner/Team Top-N 差异、profile coverage、factor 分布、residual 变化和逐日逐币种守恒。只有审核批准的新 weight/profile version 才可发布。V1 的正确性、基线和发布不等待 V2。

## 每日分摊与守恒

### 成本池和参与者

非超级节点先按以下展示/审计 pool 聚合：

```text
usage_date, currency, service_name, cost_driver_key
```

pool 由 versioned `source_pool_key` 标识，并保存输入 source-row 数、四个金额汇总、分类版本和有序 source fingerprint。该 pool 不用原始 owner/resource 标签分组；原始账本仍保留逐行资源血缘。这样既可在投影保留服务/driver 维度，又避免 bill-row × build 的交叉连接。

对一天的所有完成 Tencent 构建：

```text
W       = 全部完成构建的 active_weight（包括 unmatched）
w_i     = 一个已匹配 employee + recorded author + org/repo 的 active_weight
M       = Σ w_i
U       = W - M
```

对于同一天任一 pool 和任一可用金额字段 `P`：

```text
matched_i = P × w_i / W
residual  = P - Σ matched_i
```

`W=0` 时没有 matched 行，整个 `P` 是 `(no owner)` residual。若 `U>0`，residual 对应 unmatched 构建的份额；它保存 unmatched build count、unmatched weight 和因确定性舍入产生的 adjustment。若 `U=0`，不创建 residual，稳定排序最后一个 matched participant 吸收精度余数。退款、调整和负金额按原符号套用相同公式，不因负值排除参与者。

matched 输出至少携带 recorded author、owner、employee/group/manager、org/repo、pool、`allocation_weight`、weight model/version 和 allocation version，并固定：

```text
attribution_key    = "employee:<employee_id>"
attribution_status = "matched"
attribution_source = "tencent_ci_build_weighted"
```

超级节点 direct 行的 `attribution_source` 为 `tencent_l3_direct_<resolver-path>`（如 `owner_github`、`author_email` 或 `unmatched`），从而保留 direct 身份匹配的 provenance。residual 使用：

```text
owner              = NULL
attribution_key    = "unattributed"
attribution_status = "unattributed"
attribution_source = "tencent_ci_build_weighted_residual"
employee/group/manager = NULL
```

`(no owner)` 是此组合在审计报告和产品文案中的显示标签，不是写入 `owner` 的字面量；这保持现有 `unattributed`/`owner IS NULL` 及 resource-serving 的兼容语义。它是明确定义的费用归宿，不会按剩余员工权重二次分摊。

### 金额与精度契约

`list_cost`、`effective_cost`、`credit_amount`、`net_cost` 各自独立分摊和验证，内部以 Decimal 保留至少 9 位小数。每一可用列的最后一笔确定性输出吸收余数，因此每个 `(usage_date, currency)` 严格满足：

```text
Tencent raw ledger total = direct supernode output + shared matched output + shared (no owner) residual
```

验证也按 pool 输出，便于定位问题；发布门槛是逐日逐币种总额为零差异。`credit_amount` 当前没有 Tencent 明确的 component 语义时必须端到端保持 `NULL`，不能把未知值写成 `0`。某金额列混合“已知/未知”而不能无损表示时，阻断发布而非用 `COALESCE(..., 0)` 伪造守恒。

## 版本、staging、发布与回滚

### 不可变版本和最小持久化面

V1 的最小新增持久化面都位于 Cost Insight：

1. `cost_sources` 的 `attribution_write_mode` source policy、原始 summary ledger 的 Tencent 稳定代码和 classification/version 审计字段，以及按日 ledger/build/roster stale state；
2. 只追加的 `tencent_cost_classification_rule_set` artefact；
3. immutable allocation manifest、roster snapshot、每日 input/pool/build fingerprint，以及保存**完整 projection row**的 versioned allocation staging/history；
4. `(vendor, account_id, usage_date)` 的 Tencent publication pointer 和只追加的 publication event history。

V2 被批准后才增加 weekly immutable job-profile snapshot。它不是 V1 migration 或任务的前置条件。没有 CI Dashboard 的代码、SQL、API 或 schema 变更；Cost Insight 只读既有 `ci_l1_builds` 数据。

#### Projection row contract

staging/history 的 row 与最终 `cost_attribution_daily` 写入使用同一份完整 projection contract，不能只保存金额后在发布时猜回维度。除既有 serving 字段外，Cost Insight 必须为 Tencent projection 持久化 `allocation_version`、`classification_version`、`weight_model`、`weight_version`、`allocation_weight`、`source_pool_key` 和 `source_summary_row_hash`；`dimension_hash` 包含完整 visible/lineage identity 和 allocation version。这样 published row 可以从 pointer 追到 immutable output，而 history 可在同一 date 保存多个 version。

- **supernode direct**：复制 source summary fact 的 `service_name`、`sku_name`、`usage_type`、`cost_driver_key`、`region`、`resource_name`、`vendor_tags_json`、以及可用的 `org/repo/target_branch` 和 billing metadata；`source_allocation_scope=tencent_l3_direct`、`source_rows=1`、`source_summary_row_hash` 为该事实 hash、`source_pool_key=NULL`、`allocation_weight=NULL`。`allocation_version`/`classification_version` 仍写入，以便审计本次完整 projection。
- **non-supernode shared**：一行只代表一个 `(usage_date, currency, service_name, cost_driver_key, source_pool_key, recorded author, resolved recipient, org, repo)` 输出。`source_allocation_scope=tencent_ci_shared`，`source_rows` 是 pool 的原始行数，`source_summary_row_hash=NULL`，`source_pool_key` 非空。因为 pool 跨原始账单维度，`sku_name`、`usage_type`、`region`、`resource_name`、billing `service/project/service_exec_id` 均为 `NULL`；`vendor_tags_json` 只写 canonical allocation metadata（pool key、class/version、raw source fingerprint），不选择性复制某一行 tag。`org/repo/author` 是 CI 构建维度而不是原始账单标签。raw labels 和每条资源维度仍只在 Tencent ledger 中保留。
- **residual**：使用同一 shared pool metadata 和 `source_pool_key`，但 `recorded author/org/repo=NULL`、owner/employee/group/manager 为 `NULL`，并使用上文的 explicit residual attribution fields。所有 `NULL` 都是有意的“pool 不保留此 grain”，不是未知值被转换为零。

`allocation_version` 是不可变 manifest ID，而非 “MAX(version)” 的约定。manifest 固定：账户、日期范围、classification version/rule-set hash、weight model/version、可选 profile snapshot set、roster snapshot、build input fingerprint、raw-ledger/pool fingerprint、算法/rounding version 和创建者/时间。相同 ID 只能重试尚未 sealed 的相同输入；任何规则、profile、roster、账本或构建输入变化都需要新版本。

每日 staging 状态为 `staged -> validated -> published`；失败状态只描述该版本的该日，不能改变 active pointer。已 published 或曾 published 的版本不可就地更新、覆盖或作为普通 cleanup 删除。保留期外的历史清理需要确认它既非 active、也不在允许的 rollback 集合中。

### 独立作业和职责

1. **`publish-tencent-cost-classification`（control plane）**
   - 只发布 immutable rule-set artefact 并按分类契约重新分类完整账本分区、标记变更 usage date stale；不调用 Tencent API、不计算权重、不写 staging 或 `cost_attribution_daily`。

2. **`sync-tencent-billing-summary`（原始导入）**
   - 仅导入/修正 Tencent L3 原始账本、进行 D+5/月结账本核对，并用当前已发布 rule set 写入稳定代码分类；不计算权重、不写 allocation staging、不写 `cost_attribution_daily`。
   - 在一个 bill partition 完整提交后，比较受影响 source rows 的前后 fingerprint，标记其全部实际 `usage_date` stale。D+5 新增或金额更正走同一标记规则；未变更的 D+5 probe 不制造无效重算。
   - page/in-flight partition 不可被分类重放、materialize 或发布。若导入失败，已发布 projection 和 publication pointer 不变；materializer 只接受完整 source partition 的输入。

3. **`refresh-tencent-ci-build-staleness`（输入 detector）**
   - 只比较 `ci_l1_builds` 的每日 fingerprint 与已记录 fingerprint 并标记 stale；不读 Tencent API、不改账本、staging、projection 或 pointer。

4. **`refresh-tencent-ci-job-profiles`（仅 V2）**
   - 每周固定 CI spec revision 生成 profile snapshot；不读账单、不改 allocation pointer、不改 Dashboard。

5. **`materialize-tencent-ci-cost-allocation`（分摊计算）**
   - 接受任意显式 usage-date 范围以及新的 allocation/weight/profile version，且永远不发布。它只读已完成的原始账本、构建和 roster，生成 immutable staging；不重新调用 Tencent API。
   - 先验证分类覆盖、输入 fingerprint、build/roster/profile snapshot、一对一 direct 输出、projection row contract、residual 规则和四个金额列的守恒，才把每日版本标记为 `validated`。
   - 调度器选择 stale 日期或操作员显式范围；导入作业本身不调用它。对已发布日期的 build 输入变化，运行该作业产生新版本而非改写旧版本。

6. **`publish-tencent-ci-cost-allocation`（发布）**
   - 只接受已验证的 staged version/date，执行下面的按日原子发布；不导入、不重新分类、不重新计算权重。它可发布同一验证 version 的任意日集合，逐日独立事务。

示意命令（只说明操作契约，不是本轮实现）：

```text
cost-insight materialize-tencent-ci-cost-allocation \
  --start-date YYYY-MM-DD --end-date YYYY-MM-DD \
  --weight-model build-count-duration-v1 \
  --allocation-version <new-immutable-id>

cost-insight publish-tencent-ci-cost-allocation \
  --allocation-version <new-immutable-id> --usage-date YYYY-MM-DD
```

### 按日原子发布

`publish-tencent-ci-cost-allocation` 只能发布已验证且输入仍与 manifest 匹配的一天。每个 `(tencent, 100050658403, usage_date)` 取得互斥发布锁，并在一个数据库事务中：

1. 锁定当天 publication pointer，重新比较 raw-ledger/classification 与 build input fingerprint；若导入或构建数据已变化则拒绝发布，保持 stale；
2. 替换该日该账号的完整 Tencent projection（不是只追加 shared 行）；
3. 将当天 pointer 切换到新 allocation version，写入不可变 publication event，并清除仅在输入匹配时才可清除的 stale 标记；
4. 提交。

事务失败会同时回滚 projection 替换和 pointer 更新，Dashboard 继续看到旧的完整版本。发布后所需的 resource-serving 派生物可独立失效/重建，但不得反向触发腾讯账本导入或重新计算；其失败不改变已提交的 Tencent attribution projection。

回滚是同样的按日原子操作：选择仍保留且已验证的旧版本，重放其完整 Tencent 输出并把 pointer 切回，追加 rollback event。若当前原始账本已经不同，回滚后该日仍标记 stale 并告警，而不是把过期版本伪装为最新数据。

## 幂等性、性能和故障边界

- staging 的替换单位是一个未 sealed 的 `(allocation_version, usage_date)`；非重叠范围可安全重试。published 版本永不被 rerun 改写。
- 原始导入以 Tencent L3 page/checkpoint 为恢复单位；分摊以 usage date 为恢复单位。二者没有“导入失败后半分摊”的耦合路径。
- 构建权重先按日聚合一次，再复用于各成本 pool；不执行 bill-row × build SQL cross join。输出复杂度约为 `daily pool × matched participant` 加一个 residual，而非 `L3 component × build`。
- 默认只处理 stale 或操作员显式指定的日期；新逻辑/profile/roster version 可以全历史 shadow，再选择单日或日期范围发布。build stale detector 覆盖所有已有 Tencent date，不能把“近期稳定窗口”当作未检测旧日期的替代品。
- 发布前记录 input/output 行数、pool 数、completed/matched/unmatched build 数与权重、profile coverage、每金额列守恒差和耗时。对构建日查询和每日 fingerprint detector 必须在真实数据上做 `EXPLAIN ANALYZE`。本设计不夹带 CI Dashboard index 修改；若现有访问路径不满足运行窗口，应单独提出并评审 Dashboard 性能变更，而不是在此方案中隐式修改它。

## 验证与上线计划

1. **代码分类 gate**：连续 7 天按稳定代码元组核对 L3 与控制台超级节点金额；无 `unclassified`、无普通资源误排、无超级节点漏入共享池。发布新 rule set 后验证 control-plane job 只重新分类有变化的完整 partition，并精确标记其 usage date stale。
2. **范围 gate**：报告整个账号的 `non_supernode` 金额，证明普通 TKE、存储、COS、网络和其他服务均被纳入；额外报告 direct supernode 金额。带 author/owner 标签与无标签的非超级节点金额都必须进入同一共享输入。
3. **身份/时间 gate**：抽样 `/test`、retest、PR、Jenkins 构建和 supernode direct 标签，确认同一 Tencent snapshot resolver 的 alias/GitHub/email/normalized/ambiguous 结果、`ci_l1_builds.author` 的产品语义、UTC→上海日期边界、完成构建筛选和缺失 duration 的 `1` 权重；报告 matched 与 unmatched 权重。
4. **V1 shadow**：对代表性日期生成新 version 且不发布。核对每个 pool、日、币种的四个金额列、direct source hash、一对一超级节点输出、matched/residual 权重和 `(no owner)` 金额，以及 projection row contract 中的 NULL/non-NULL lineage fields。
5. **重放/更正/staleness**：重复 materialize 同一 unsealed version 验证幂等；模拟 D+5 新行/金额更正和 classification 修正，确认只更新原始账本并标 stale，旧 projection 持续可读，新 version 不需重跑导入。修改已完成构建的 author、时长、完成时间或 phase，确认每日 fingerprint detector 精确标 stale、不会改写已发布版本。
6. **通用 pipeline gate**：启用 active Tencent source 后，验证无参数 generic refresh 跳过它；显式 Tencent refresh 在任何 delete/job-state/publication 写入前失败；generic derived materialization 的 max-date、source window、denominator 和 global publish 都不含 Tencent。验证 Tencent 单日发布不改变 `cost_allocation_publication`，也不触发全历史 derived rebuild。
7. **发布/失败/回滚**：注入 validation、projection-write 和 pointer-write 失败，确认没有部分日投影；发布后回滚单日，确认旧 version 被保护、pointer/history 可审计，且账本已变化时 stale 仍存在。
8. **V2 shadow（可选）**：固定 weekly snapshots，比较 V1/V2 Top Owner/Team、profile coverage、factor 分布、residual 和逐日逐币种守恒；未审核不得发布。
9. **性能 gate**：记录实际 `EXPLAIN ANALYZE`、输入/输出行数、pool/participant 基数、fingerprint scan 和每日期耗时；不通过则停止上线并另开最小范围的性能任务。

## 非目标与删除条件

- 不以 Pod 实测使用率、Pod 生命周期或动态 Pod 数量分摊 V1；不把不可验证 YAML 解释为资源用量。
- 不按账单原始 `author`/`owner` 给非超级节点做例外，也不把 residual 再次分给已匹配员工。
- 不在 Dashboard/API 读路径计算，不新增 Dashboard allocation 参数，也不改 CI Dashboard。
- 不把这个临时 Tencent 权重模型扩展成通用 FinOps 框架。

当腾讯超级节点账单稳定提供 Pod 级 `author/org/repo` 标签，并在 D+5 和月结账本核对中证明可用后：停止 `materialize-tencent-ci-cost-allocation`，保留必要历史审计，改由原始账单 direct attribution 成为唯一来源。这个删除条件防止临时权重方案被永久化。
