# Daily Data Freshness Check & Alert Design

## 目标

每天运行一次检查 Job，验证 ci-dashboard 和 cost-insight 各核心数据表的新鲜度是否在容忍范围内。超过阈值则通过飞书发送告警。

## 检查范围

跳过 raw 表（如 `cost_raw_details`、`ci_l1_pod_events`），只检查汇总层和直接消费的表。上游外部表（`prow_jobs`、`github_tickets`）作为独立检查项纳入，因为它们的数据新鲜度直接影响下游所有表。

## 检查项总览

| # | 检查对象 | 数据来源 | 新鲜度指标 | 容忍 Lag | 告警级别 |
|---|---------|---------|-----------|---------|---------|
| 1 | `ci_l1_builds` | Kafka Jenkins CDEvents + prow_jobs sync | `MAX(start_time)` vs NOW | **4 小时** | HIGH |
| 2 | `ci_l1_pod_lifecycle` | K8s Watch + GCP Cloud Logging sync | `MAX(last_event_at)` vs NOW | **4 小时** | HIGH |
| 3 | Archive Error Logs 积压 | Jenkins API → GCS | 近 4 小时内未归档的失败 build 数量 | **4 小时** 窗口内积压 ≤ 动态阈值 | MEDIUM |
| 4 | `prow_jobs` | 外部 Prow Reporter（Prow SQL 同步） | `MAX(startTime)` vs NOW | **4 小时** | HIGH |
| 5 | `github_tickets` | 外部 GitHub 数据同步（tibuild） | `MAX(updated_at)` vs NOW | **30 小时** | MEDIUM |
| 6 | `ci_l1_flaky_issues` | GitHub Issues API (daily) | `ci_job_state` 中 `ci-sync-flaky-issues` 的 `last_succeeded_at` | **30 小时** | MEDIUM |
| 7 | `ci_l1_pr_events` | github_tickets → sync-pr-events | `MAX(event_time)` vs NOW | **4 小时** | MEDIUM |
| 8 | `problem_case_runs` | cloudevents-server CloudEvents | `MAX(report_time)` vs NOW | **4 小时** | MEDIUM |
| 9 | `ci_l1_builds` 派生列 | refresh-build-derived | `ci_job_state` 中 `ci-refresh-build-derived` 的 `last_succeeded_at` | **4 小时** | MEDIUM |
| 10 | `cost_bq_export_summary_daily` | GCP/AWS Billing Export（daily） | `MAX(usage_date)` vs NOW（GCP 源） | **4 天** | MEDIUM |
| 11 | `cost_attribution_daily` | cost_raw_details + roster（daily） | `MAX(usage_date)` vs NOW | **4 天** | MEDIUM |
| 12 | `cost_unmatched_resource_daily` | GCP/AWS Unmatched Detection（weekly） | `MAX(usage_date)` vs NOW | **10 天** | LOW |
| 13 | `sync-gcs-cache-last-seen` | BigQuery GCS Audit Logs → BigQuery last-seen 表 | `cost_job_state` 中 `sync-gcs-cache-last-seen` 的 `last_succeeded_at` | **30 小时** | LOW |
| 14 | `roster_employees` | Lark 飞书通讯录 API（daily） | `MAX(updated_at)` vs NOW | **30 小时** | MEDIUM |

## 详细检查逻辑

### 1. `ci_l1_builds` — 最新 Build 时间

```sql
SELECT MAX(start_time) AS latest_build_time
FROM ci_l1_builds
WHERE start_time IS NOT NULL;
```

如果 `latest_build_time < NOW() - INTERVAL 4 HOUR` → 告警。

> **注意：** 夜间/周末可能没有 CI 活动，告警需要配合工作时段判断（例如只在 08:00-22:00 UTC+8 触发 HIGH 级别，其余时段降级为 LOW 或忽略）。先实现为全天 4 小时检查，后续根据误报频率调整。

### 2. `ci_l1_pod_lifecycle` — 最新 Pod 事件时间

```sql
SELECT MAX(last_event_at) AS latest_pod_event
FROM ci_l1_pod_lifecycle
WHERE last_event_at IS NOT NULL;
```

如果 `latest_pod_event < NOW() - INTERVAL 4 HOUR` → 告警。

> 注意：与 #1 相同，需要考虑夜间低活跃期。

### 3. Archive Error Logs — 积压检查

```sql
SELECT COUNT(*) AS pending_count
FROM ci_l1_builds
WHERE state IN ('failure', 'error', 'timeout', 'aborted')
  AND log_gcs_uri IS NULL
  AND completion_time > NOW() - INTERVAL 4 HOUR;
```

如果 `pending_count > 0`（近 4 小时内产生的失败 build 都还没有被归档）→ 告警。

> **阈值说明：** `archive-error-logs` 每小时跑一次，且只处理未归档的 build。正常情况下，失败 build 产生后最多 1 小时就会被归档。如果 4 小时窗口内积压大于 0，说明归档 job 可能停滞。

### 4. `prow_jobs` — 最新 Prow Job 时间

```sql
SELECT MAX(startTime) AS latest_prow_job
FROM prow_jobs
WHERE startTime IS NOT NULL;
```

如果 `latest_prow_job < NOW() - INTERVAL 4 HOUR` → 告警。

> `prow_jobs` 由外部 Prow Reporter 写入，是 `ci_l1_builds` 的上游数据源（通过 `sync-builds` job）。如果这个表停滞，`ci_l1_builds` 的 Prow 来源数据就会缺失。夜间/周末与 #1 相同的低活跃期问题。

### 5. `github_tickets` — 最新 GitHub Ticket 更新时间

```sql
SELECT MAX(updated_at) AS latest_ticket_update
FROM github_tickets;
```

如果 `latest_ticket_update < NOW() - INTERVAL 30 HOUR` → 告警。

> `github_tickets` 由外部 GitHub 同步服务（tibuild）写入，是 `ci_l1_pr_events` 和 `ci_l1_flaky_issues` 的源表。GitHub 上每天都有大量 PR/issue 活动，30 小时无更新说明同步可能断了。

### 6. `ci_l1_flaky_issues` — Flaky Issue 同步

```sql
SELECT last_succeeded_at
FROM ci_job_state
WHERE job_name = 'ci-sync-flaky-issues';
```

如果 `last_succeeded_at < NOW() - INTERVAL 30 HOUR` → 告警。

### 7. `ci_l1_pr_events` — 最新 PR Event 时间

```sql
SELECT MAX(event_time) AS latest_pr_event
FROM ci_l1_pr_events
WHERE event_time IS NOT NULL;
```

如果 `latest_pr_event < NOW() - INTERVAL 4 HOUR` → 告警。

> 注意：如果 PR 事件写入频率本身就不高（例如夜间），可能误报。后续可根据实际数据调整阈值或增加工作时段判断。

### 8. `problem_case_runs` — 最新 Test Case Run 时间

```sql
SELECT MAX(report_time) AS latest_report_time
FROM problem_case_runs
WHERE report_time IS NOT NULL;
```

如果 `latest_report_time < NOW() - INTERVAL 4 HOUR` → 告警。

### 9. `ci_l1_builds` 派生列刷新 — 通过 job_state

```sql
SELECT last_succeeded_at
FROM ci_job_state
WHERE job_name = 'ci-refresh-build-derived';
```

如果 `last_succeeded_at < NOW() - INTERVAL 4 HOUR` → 告警。

### 10. `cost_bq_export_summary_daily` — Billing 汇总最新日期

```sql
SELECT MAX(usage_date) AS latest_usage_date
FROM cost_bq_export_summary_daily
WHERE vendor = 'GCP';
```

如果 `latest_usage_date < CURDATE() - INTERVAL 4 DAY` → 告警。

> GCP billing export 本身有 1-2 天延迟，加上每日 sync job，4 天容忍度覆盖了 1 次 sync 失败 + billing 数据源延迟的缓冲。AWS 源单独检查需要更长的容忍度（~5 天），可后续按需添加。

### 11. `cost_attribution_daily` — 成本归属最新日期

```sql
SELECT MAX(usage_date) AS latest_usage_date
FROM cost_attribution_daily;
```

如果 `latest_usage_date < CURDATE() - INTERVAL 4 DAY` → 告警。

> 依赖链路：GCP billing export → `cost_raw_details` → `cost_bq_export_summary_daily` → `cost_attribution_daily`。只要 `cost_bq_export_summary_daily` 是新鲜的，`cost_attribution_daily` 也应该新鲜。此项作为确认检查。

### 12. `cost_unmatched_resource_daily` — 未匹配资源最新日期

```sql
SELECT MAX(usage_date) AS latest_usage_date
FROM cost_unmatched_resource_daily;
```

如果 `latest_usage_date < CURDATE() - INTERVAL 10 DAY` → 告警。

> 这是周级 job，且数据本身不频繁变动。10 天容忍 1 次周级失败 + 缓冲。

### 13. `sync-gcs-cache-last-seen` — GCS Cache 访问日志同步

```sql
SELECT last_succeeded_at
FROM cost_job_state
WHERE job_name = 'sync-gcs-cache-last-seen';
```

如果 `last_succeeded_at < NOW() - INTERVAL 30 HOUR` → 告警。

> `sync-gcs-cache-last-seen` 将 GCS audit log 数据写入 BigQuery last-seen 表（不在 TiDB 中），每日运行。无法直接查询目标表的新鲜度，只能通过 `cost_job_state` 间接检查。此项依赖 cost-insight 数据库连接。

### 14. `roster_employees` — 员工花名册最新更新时间

```sql
SELECT MAX(updated_at) AS latest_update
FROM roster_employees;
```

如果 `latest_update < NOW() - INTERVAL 30 HOUR` → 告警。

> `roster-sync` 每天 03:00 运行，30 小时容忍下一旦失败可以再等一天。

## 告警输出格式

飞书消息示例：

```
📊 Daily Data Freshness Check — 2026-06-25

🔴 HIGH (1):
  • ci_l1_builds: latest build at 2026-06-25 02:15, lag = 6h 30m (threshold: 4h)

🟡 MEDIUM (2):
  • ci_l1_flaky_issues: last sync at 2026-06-23 02:00, lag = 32h (threshold: 30h)
  • roster_employees: last update at 2026-06-23 03:00, lag = 31h (threshold: 30h)

✅ All clear (11): ci_l1_pod_lifecycle, archive_error_logs, prow_jobs, github_tickets,
   ci_l1_pr_events, problem_case_runs, ci-refresh-build-derived,
   cost_bq_export_summary_daily, cost_attribution_daily,
   cost_unmatched_resource_daily, sync-gcs-cache-last-seen
```

## 实现计划

### 新增文件

| 文件 | 用途 |
|------|------|
| `ci-dashboard/src/ci_dashboard/jobs/check_data_freshness.py` | 检查逻辑实现 |
| `ci-dashboard/scripts/render_data_freshness_check_cronjob.sh` | CronJob YAML 渲染脚本 |

### CLI 注册

在 `ci_dashboard/jobs/cli.py` 中注册子命令：

```python
subparsers.add_parser("check-data-freshness", help="Daily data freshness check and alert")
```

### CronJob 调度

```
Schedule: 0 7 * * *  # 每天北京时间 07:00（UTC+8）
```

早上 7 点执行，这样前一天的数据和凌晨的 daily job 都已经完成，且有足够时间在上班前处理告警。

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LARK_ALERT_CHAT_ID` | 告警发送目标飞书群 ID | 必填 |
| `FRESHNESS_DRY_RUN` | 只打印不发送 | `false` |
| `FRESHNESS_SKIP_WORK_HOUR_CHECK` | 跳过工作时段判断 | `false` |

## 数据库连接

检查 Job 需要访问两个 MySQL 数据库：

| 数据库 | 检查的表 |
|--------|---------|
| ci-dashboard DB | `ci_l1_builds`, `ci_l1_pod_lifecycle`, `ci_job_state`, `ci_l1_flaky_issues`, `ci_l1_pr_events`, `problem_case_runs`, `prow_jobs`, `github_tickets` |
| cost-insight DB | `cost_bq_export_summary_daily`, `cost_attribution_daily`, `cost_unmatched_resource_daily`, `cost_job_state` |
| roster DB（可能与 ci-dashboard 同库） | `roster_employees` |

ci-dashboard 和 cost-insight 可能使用不同的 TiDB 实例或同一实例的不同 database。需要确认连接方式（两个 engine 分别连接还是共用一个连接池跨库查询）。

## 待确认项

1. **ee-ops 中的实际 CronJob 频率**：`sync-builds`、`sync-pr-events`、`refresh-build-derived`、所有 cost-insight job 的 CronJob 定义在 ee-ops 仓库。拿到实际频率后可微调容忍阈值。
2. **飞书群 chat_id**：需要确定告警发送到哪个群。
3. **夜间降级策略**：`ci_l1_builds`、`ci_l1_pod_lifecycle` 和 `prow_jobs` 在夜间（22:00-08:00）CI 不活跃时可能产生误报。是否需要按时间段降级告警级别？
4. **ci-dashboard 和 cost-insight 数据库连接**：确认两个数据库是否在同一个 TiDB 实例上，以及 Job 如何配置多库访问。
5. **`github_tickets` 同步频率**：确认 tibuild/chatops-lark 写入 `github_tickets` 的实际频率（实时/定时），以校准 30h 容忍度是否合理。
