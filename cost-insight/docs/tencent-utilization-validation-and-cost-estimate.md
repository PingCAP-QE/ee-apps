# 腾讯云 CICD 利用率：关联验证与月度增量成本估算

只读验证日期：2026-11-23。业务口径更新：2026-11-24，日级持久化、每天四次刷新，**repo + 稳定 Job + 资源类型 / 规格**为优化维度，不做个人利用率或磁盘使用率；不是部署结果或账单承诺。
范围：**仅账号 `100050658403`**，不考虑组织其他账号。
关联设计：[资源利用率采集](resource-utilization-design.md)、[保留 / 自动清理 / 防积压](resource-utilization-retention-design.md)。费用与存量估算以清理和门禁随采集一起上线为前提。

## 1. 结论

1. **CI Pod CPU / memory / requests 可以直接读取，已关联真实 CI build / repo。** 不需要高频自采、安装新 Agent 或开通 Prometheus。
2. **需要补齐的是 repo / Job 证据，不是个人信息。** 样本 498 个 CI Pod 中，277 个匹配 lifecycle，228 个有 repo / build URL；一周已记录 Pod 的 repo / build URL 约 53% 有值。author 约 51% 只是历史观察，已不再是本功能依赖或门槛。
3. **只保存日摘要**：资源层为机器 / 节点，消费层为 repo + 稳定 Job / workload + 资源类型 / 规格；北京时间 `00:30、06:30、12:30、18:30` 重算当天 / 前一天后替换。不保存逐 Pod 使用流水，不新采磁盘指标，不复制个人归属；原 cost 个人分账不变。
4. 实测外推约 31 万 CI Pod-day/月、38 万 Pod-hour/月是输入规模，不是最终表行数。去个人后的稳定组合基数待测，旧小时 / 含磁盘预留的估算不再适用。去磁盘逐 Pod-day 替代参照约 **64 万新增 / 月、14 天约 30 万保留行**，并非最终业务聚合测量或严格上界；暂预留 1–2 GiB。
5. **暂沿用 ¥100/月作为待校准预算，不把旧 ¥20–80/月区间冒充新日流程的实测报价。** 日重算减少持久化明细，但会重复读取历史点，API、CPU、DB RU 不一定按刷新次数成比例下降。当前 DB 套餐、实际日组合基数与重算耗时仍需 canary 校准。

## 2. 已验证的端到端链路

接口：`monitor:DescribeStatisticData`，namespace `QCE/TKE2`。
条件：cluster + 小批量 `un_instance_id in (...)`；一次请求可带多个 MetricNames。

2026-11-23 北京时间 20:45—21:05，58 个节点 ID，6 次分批请求、每次最多 10 个节点，约 8 秒完成云 API 读取：

| 项目 | 结果 |
| --- | ---: |
| 返回有效 Pod（包含系统 Pod） | 971 |
| CI Pod，namespace 为 `jenkins-*` | 498 |
| CI CPU 实际量有效点 | 3,691 |
| CI working set 有效点 | 3,691 |
| CI CPU/request 有效点 | 3,691 |
| CI working set/request 有效点 | 3,691 |
| 四指标合计 JSON 响应 | 约 5.22 MB，含系统 Pod、维度及缺点位置 |
| CI Pod 匹配现有 lifecycle | 277 / 498 |
| 其中有 author | 226 / 498 |
| 其中有 repo、build URL | 各 228 / 498 |

匹配使用 `(source/cluster, namespace, pod_name)` 获取现有 Pod UID 和观测区间，再通过 normalized build URL 查询 `ci_l1_builds`。
监控响应没有 Pod UID，正式实现必须校验时间区间和名称重用，不能只凭 pod_name 永久关联。
这不是随机抽到一个相似名称：下面的 build URL、repo、author 已用数据库实际记录交叉核对。

### 真实例子

Pod：`jenkins-tidb/pingcap-tidb-ghpr-mysql-test-3290-20t10-dv3lv-dhq33`。

- author：`wjhuang2016`。
- repo：`pingcap/tidb`。
- build：<https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_mysql_test/3290/>。
- `ci_l1_builds.id=50796545`，`cloud_phase=TENCENT`，author / repo / URL 一致。
- 该窗口内此 Pod 有 6 个有效分钟点：

| 指标 | 样本平均 | 一分钟均值的最大值 |
| --- | ---: | ---: |
| CPU cores | 1.70 | 3.01 |
| CPU / requests | 81.00% | 143.12% |
| working set / memory requests | 27.65% | 86.67% |

这些是**样本窗口统计，不是整个任务最终统计**。CPU / requests 超过 100% 合法，不应截断。
另已交叉核对 realcluster build `2659`、ghpr check build `3279` 的 Pod / build / author 关联。

### 当前覆盖缺口

以下 author 栏和例子里的个人信息仅保留既有只读验证证据，不表示新表会复制这些字段，也不构成利用率上线条件。
2026-11-16—22，北京完整一周，现有数据库：

| 项目 | 结果 |
| --- | ---: |
| Tencent CI builds | 10,414 |
| 已记录 Jenkins lifecycle Pod | 56,407 |
| 有 metadata / build URL | 30,241，53.61% |
| 有 author | 28,867，51.18% |
| 有 repo | 30,155，53.46% |

这是一周已记录 Pod 的字段覆盖率，不是按 CPU 使用量加权的归属率，也不证明所有 CI Pod 都已入库。
监控直接发现的 Pod 比 lifecycle 更多；缺口可能涉及事件 / 元数据摄取覆盖、子 Pod 关联和时延，本次没有进一步确认根因。
**上线重点：复用 repo / build / UID 关联，并新增 requests 与节点区间证据；缺 repo / Job 的使用量进入 unknown，不丢弃或硬分给已知 Job。** 现有 `ci_l1_pod_lifecycle` 不保存 nodeName、requests / limits，当前 watcher 也没有 Node-read RBAC；本次样本比率可读不等于 requests 有效区间已经落库。按主设计 §3，由既有 `ci-dashboard` Pod LIST/WATCH 接入侧新增白名单元数据出口，并在同侧复用既有集群 URL / token / CA 新增 cluster-scoped 分页 Node LIST 及其只读 get/list RBAC。Cost Insight 仅负责周切片 observation schema / sink 与最小 DB 权限，不新增 Kubernetes 客户端、Node 清单职责或集群凭据。这些均为待实现、待 canary 的新工作，不是已验证部署能力；缺过去区间不得从当前快照倒填。
不以 author / owner / roster 做匹配条件，不猜随机 Pod 名，不为本功能补个人标签；只读证据不完整时明确 coverage。

## 3. 当前规模：从监控直接量，不只数 CI build

节点按日读取 3600 秒历史统计，再统计每小时出现过有效点的节点 ID：

- 2026-11-16—22 共 9,736 个 node-hour bucket。
- 平均每小时出现约 58 个节点 ID，最低 18、最高 136。
- 这是每小时出现过的 ID 数，不是同一瞬间的节点并发；节点销毁 / 替换会产生多个 ID。
- 整周一次查询出现 2000 序列边界，拆日后计数增加；正式 collector 必须分窗 / 分批校验，不能默认整周响应完整。

再选一个工作日与一个周日，按当日已发现节点 ID 分批读取 Pod CPU 历史统计。
CPU 在这里仅用于量 Pod 规模，不用小时均值推导峰值；正式平均 / 峰值仍使用现有分钟监控。

**已验证的可读范围不等于保留期**：本报告只证明该周的 3600s 历史统计和 §2 的 20 分钟 60s Pod 样本可读，尚未验证 CVM / TKE 各指标的 60s avg 查询 horizon，不能声称分钟点可重放 7 天或 14 天。P0 / 有界 canary 必须逐来源验证 horizon、延迟和安全余量，至少满足当天 / 前一天重算；更早失败日期只在已验证窗口与未过期 / 证据可用范围的交集重试。越界保留最后可信日版本并标 stale（无版本则 missing），不把 3600s max 当作日均值或分钟峰值；周收口从已保存且契约相容的 `account_class` 日行完成，缺口可见且不延长 D。

| 日期 | 唯一 CI Pod | CI Pod-hour bucket | 全集群 Pod-hour bucket | 最大单批序列数 |
| --- | ---: | ---: | ---: | ---: |
| 2026-11-20，周五 | 13,975 | 16,930 | 30,888 | 1,437 |
| 2026-11-22，周日 | 1,478 | 1,815 | 8,447 | 664 |

CI 为这两个日期监控里出现的五个 `jenkins-*` namespace。
完整日请求无 API 错误，单批未碰到 2000 序列边界；只能证明这个采集范围，不是 API 对缺失点的绝对完整性保证。
系统 Pod 监控可能被返回；首版只把 CI Pod 输入合并成稳定消费日摘要，不默认保存逐 Pod 明细。账号总利用率通过去重节点层计算，仍包含系统开销。

按“5 个工作日 + 2 个休息日”、30 天外推：

```text
CI Pods/月 = (13,975 × 5 + 1,478 × 2) / 7 × 30 ≈ 312,133
CI Pod-hour/月 = (16,930 × 5 + 1,815 × 2) / 7 × 30 ≈ 378,343
TKE node-hour/月 = 9,736 / 7 × 30 ≈ 41,726
```

这是两天抽样的输入规模模型，不是一个月实测；Pod-hour 仅用于说明原始负载，不表示创建小时表。周六 / 其他工作日可能不同，容量先留余量。
输入包含缺 repo / Job 或 author 的 Pod；缺个人信息不能排除任何计算用量。日级落库基数取决于稳定 repo-Job-资源组合，不等于 build 数、Pod 数或原始点数。

## 4. 请求量与监控费用

### TKE

- 每天四次日刷新；CPU / memory 相关四个指标同请求读取，对当天 / 前一天的已退出节点也要查询。
- `TKE 请求数 = Σ各轮 Σ对象批次 Σ时间窗 Σ指标批次`。60 秒数据按小于 12 小时的范围拆窗，必要时进一步拆小，避开响应序列边界。
- 原小时方案的 1万—3万次 / 月不再作为现行日流程预测。不能只拿平均 58 个当前节点计算整天：工作日曾出现 464 个不同节点 ID，完整日重读需要更多对象 / 时间批次。
- 不能把 1 个请求里返回的 Pod 数、metric 数、点数乘成收费请求数。
- 当前公开 API 计费页**只明确列 `GetMonitorData` 为计费接口**，没有找到 `DescribeStatisticData` 的收费单价。
- 因此本模型对现有 `QCE/TKE2` 读取按“未发现新增收费项、预算 ¥0”处理；不是已经查到合同保证永久免费。若需要有约束力的报价，实施前向腾讯确认该接口是否收费。

TKE 已有基础监控负责采集；没有新装 agent、新增 Prometheus 上报、托管 collector 或 CLB。
**不能把可选 Prometheus 的新增采集 / 存储费用算到这条现成监控读取链路里。**

### 4 台 CVM

新的可核算场景：每轮重读前一天 + 当天截至计划时刻减 10 分钟的数据；保守统一拆成最多 2 小时时间窗，单批最多 50 个对象、单项统计时约 6000 点，低于 7200 点限制。端点重叠需去重。
四轮的总时间窗数约为 `13 + 16 + 19 + 22 = 70` 个 / 天（含前一日重复读取），不是每轮只有一个 API 请求。

```text
CVM：2 地域 × CPU / memory 两指标 × 70 窗 / 天 × 30 天 = 8,400 次 / 月
假设全部按公开单价计费（不扣免费额）：8,400 / 10,000 × ¥0.25 = ¥0.21 / 月
约 20% 重试余量后的该场景保守计费预留约 ¥0.3 / 月；非预期实付费用
磁盘：首版不做，不预算磁盘 / PVC 采集和 API 请求
```

这是现有 4 台普通 CVM 与上述拆窗策略的场景模型，不是已实现请求数 / 费用上限；免费额度足够时该场景查询费为 ¥0，额度已被其他任务耗尽且启用付费时才按实际超额调用收费。TKE 另按实际拆窗校准。旧的 400 个磁盘槽位、磁盘指标请求和空间指标验证全部从本次范围移除。
官方定价：主账号每月 100 万次免费 `GetMonitorData`；超过后需手动开通按量付费，¥0.25 / 万次。额度由主账号共享，本次未查剩余额度 / 付费启用状态。额度耗尽但未开通付费时会停读，不能假设本任务独享免费额。

资源清单按明确账号、地域、产品分页读取，不反复全地域扫描；本次未发现这些普通 Describe API 的独立按量收费项。

## 5. DB 新增行数和保留体量

最终方案将 CPU / memory / requests 的积分、avg / 日 peak / coverage 合并为日宽行，逐指标内联保存完整来源身份 / 分母契约（主设计 §4），测量 bytes/row 时必须包含这些字段；消费键使用 repo + 去掉 build 编号的稳定 Job / workload + 资源类型 / 规格，不含个人维度，不保存 Pod 利用率明细。固定日 rollup 含按去重物理层计算的 `account_class`（vendor + account_id + resource_class），作为各类别账号周汇总的唯一输入。辅助证据只存 UID、repo / Job、规格 / requests、节点与有效区间，不复制个人资料、磁盘 / PVC 关系或完整对象。

```text
日表行数 = Σ日期(物理资源日基数 + 实际出现的消费组合日基数 + 固定 rollup 日基数)
辅助表行数 = 日重算必需的不同归属 / 规格区间数
```

稳定组合基数尚未从完整标签 / job 证据统计，不能报一个确定的最终行数。为说明“换成天不会统一减少 24 倍”，保留以下**非最终方案的逐 Pod-day 参照**：

| 参照项 | 新增行 / 30 天，约 |
| --- | ---: |
| 如果仍一 Pod-day 一行 | 312,000 |
| TKE node-day：一周每日不同节点 ID 合计 2499，再按 30/7 外推 | 10,710 |
| 4 台 CVM-day | 120 |
| 固定 repo / Job / 资源 rollup 参照：200 组 / 天 | 6,000 |
| 磁盘 | 0，移出范围 |
| 参照日表小计 | 约 329,000 |
| 必要元数据保守参照 | 约 312,000 |
| **去磁盘参照合计** | **约 64 万新增 / 月，14 天约 30 万保留行** |

该参照不是最终去个人后的 repo-Job-资源基数、实测新表行数或严格上界。Pod 的 repo / Job / 规格变化可贡献多个区间，多个 Pod 也可合并成同一稳定组合；个人标签变化不能拆出新组合。随机 Pod / build 值不得混进 job key。无需再验证磁盘空间或为它预留数据行。

- 旧小时方案 120 万 / 月、55 万保留行、300–400 万次 mutation / 月，仅是已弃用方案的对照，不再作为日方案承诺。
- 暂按 1–2 GiB 留存储余量，随后测量实际组合数、区间数与索引后 bytes/row。容量 guard 是防失控条件，不是正常预测。
- 每日重算会覆盖当前日 / 前一日的多个 scope，必须删除消失的旧组合并原子发布；逻辑行不重复增加，但读 / 写 / 删除次数仍需实测，不能只数 upsert。
- 历史仍只有账号周汇总：每周约 10–20 行、约 500–1000 行 / 年；不带个人、job、资源或 Pod。
- 两张短期表同周过期，开放区间不可永久续期；已有 CI lifecycle 保留不变。独立清理与 4h 停写门禁继续生效，不因日粒度而推迟交付。

## 6. 人民币预算与不确定项

日流程尚未跑 canary，以下是**可核算场景 / 单价参数**，不是新方案已测出的全包费用。USD 换算暂用现有展示的 `6.5 CNY/USD` 作为预算参数，不是汇率报价。

| 项目 | 日方案当前口径 | 依据 / 待测项 |
| --- | --- | --- |
| 腾讯云监控查询 | 上述 CVM 场景免费额足够则 ¥0；全部按公开单价加重试余量预留约 ¥0.3 / 月，非预期实付或全流程上限 | 最终按共享额度和真实拆窗 / 重试请求计；TKE 未发现公开收费单价；不采磁盘 |
| 新增监控采集设施 | ¥0 | 复用基础监控，不新建 agent / Prometheus |
| 定时任务计算 | 依据每轮日重算耗时校准 | 举例四轮 / 天、每轮 15 分钟、0.5 CPU / 1 GiB，共 30 job-hours / 月；假设 CPU $0.05/vCPU-hour + memory $0.01/GiB-hour，等价约 ¥6.8 / 月；非当前合同或已测运行时 |
| DB 存储 | 先留 1–2 GiB，参考约 ¥1–5 / 月 | Starter 公布约 $0.20–0.38/GiB-month；当前 DB 套餐 / bytes per row 仍待确认 |
| DB 读写 / 查询 | 待实际日组合基数、重算和清理 RU | 若适用 Starter，参考 `额外月 RU / 100万 × $0.10–0.12 × 6.5`，不沿用小时方案 RU 量假设 |
| 网络 | 待测新流程字节和实际端点 | 整日重读可能增加原始响应量；DB 只写摘要，但不能因此声称所有网络费用减少 |
| **预算** | **暂预留 ¥100 / 月，canary 后重估** | 不是费用上限或已验证总价；旧 ¥20–80 区间不作为日流程报价 |

注意：

1. GKE 当前为可见 VM 节点的集群，不把 Autopilot 的 Pod 计费方式误当实际账单。已有节点有空闲时，短任务现金增量可为零，但仍占用资源；若触发整节点扩容，要按节点生命周期另外计算。
2. TiDB 已连接并只读查询了业务数据 / information_schema，但**没有读取实际 DB 合同 / 计费控制台，也没有执行写压测**。因此 RU 与每行大小是预算假设，不能从“行数”直接证明最终费用。
3. Starter 价格仅作资源量级参照；若当前为 Dedicated / Essential / 内部托管或固定套餐，必须按实际计划换算，不套用不存在的 RU 账单。不额外把数据库内置复制倍数重复乘到公开存储单价上。
4. 现有公开 Starter RU 规则计入写请求、写字节、SQL CPU、读流量等，写还包含底层副本 / 索引；不能说一次 SQL 或一行就等于一个 RU。预算以批处理、少量必要索引为前提。
5. 本次 20 分钟、包含系统 Pod 的响应约 5.22 MB，不能直接外推四次整日重算的月字节数。按 namespace 筛选、拆窗、有界内存处理，原始点仍不入库；Google 入站免费不代表 DB 出站、查询资源及所有端点均免费。
6. 日组合行数和辅助证据基数尚未测量；去磁盘约 64 万 / 月只是逐 Pod-day 替代参照，不能用来保证容量或费用。流量翻倍、标签变化、全日重算会增加开销；停写门禁不取消。若触发节点 / DB 整档扩容，¥100 预算不是费用上限。

### 上线后的最小校准

元数据依赖由 ci-dashboard 接入侧负责：复用已有 Kubernetes URL / token / CA 与客户端，新增 cluster-scoped Node LIST 路径及该身份只读 Node RBAC，与 Pod 白名单出口一起交付。Cost Insight 仅接收证据，不新增集群凭据；新增 LIST、sink 的资源 / DB 开销也纳入校准。

第一阶段只做有界 canary：先验证各来源 60s avg 的可查询 horizon / 延迟 / 安全余量、白名单元数据出口 / Node RBAC 与 requests 区间覆盖，再按四个刷新时刻实际重算当天 / 前一天，测量稳定 job 组合日基数、含 `account_class` 的固定 rollup 数、辅助区间数、每轮 API 次数 / 字节、CPU / memory / duration、日覆盖替换的 INSERT / UPDATE / DELETE、含指标契约的索引后 bytes/row、清理吞吐，以及实际 DB RU（若该计划支持）。模拟过源窗口保留 stale / missing 和按类别仅合并相容日行的最终收口，不把设计验收当作已执行结果。
确认未把 build / Pod 随机值混入 job key、个人未进入任何利用率分组键、没有因缺 author 排除用量，也未丢弃 unknown repo / Job；不再为磁盘留槽位。把观测替换到当前日流程模型，再决定正式预算。不为验证开启长期全量写入或新收费能力。

## 7. 来源与证据

- [腾讯云监控 API 计费](https://cloud.tencent.com/document/product/248/77914)：GetMonitorData、100 万次免费、¥0.25 / 万次、手动启用超额付费。
- [DescribeStatisticData](https://cloud.tencent.com/document/product/248/51845)：多指标 TKE 查询；20 TPS 是限流，不是计费。
- [TKE 基础监控](https://cloud.tencent.com/document/product/457/34181)。
- [TKE monitor agent](https://cloud.tencent.com/document/product/457/66815)：已有节点 / Pod 采集。
- [可选托管 Prometheus 计费](https://cloud.tencent.com/document/product/248/87089)：与本设计区分。
- [TiDB Cloud Starter 价格及 RU 定义](https://www.pingcap.com/tidb-cloud-starter-pricing-details/)：仅作参考，非当前 DB 合同确认。
- [Google Cloud 网络计费](https://cloud.google.com/vpc/network-pricing)。
- 数据库：`ci_l1_pod_lifecycle`、`ci_l1_builds`；只执行有时间边界 / 索引的 SELECT 和 schema / 索引检查。
- 监控样本 RequestId：`f3c686ee-9f11-4db0-a65e-0b7c15e20735`；工作日节点范围 `e36eb917-88a8-43ad-bf23-83d33d51a0a1`；周日节点范围 `5165b928-55b3-4d98-bd03-631db6812c64`。

云端没有开通付费、修改 IAM、创建证书、安装 Agent、部署任务或写入业务数据。临时原始响应不提交到仓库。
