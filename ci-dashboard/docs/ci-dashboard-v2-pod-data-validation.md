# CI Dashboard V2 Pod Data Validation

Status: Pre-execution draft

Last updated: 2026-04-21

Scope:
- validate correctness and completeness of pod data after `sync-pods` has been running in production
- validate derived lifecycle timing against raw source evidence
- validate build linkage quality and coverage
- validate that the data can reliably answer the V2.1 questions

This document complements:
- `ci-dashboard-v2-pod-validation-plan.md` (operational/structural validation)
- `ci-dashboard-v2-pod-question-map.md` (what questions the data should answer)

## 1. Validation Objectives

1. **Source fidelity**: are Cloud Logging events being captured completely and accurately?
2. **Derivation correctness**: are lifecycle timestamps and durations computed correctly from raw events?
3. **Build linkage quality**: do pod lifecycle rows reliably map back to the correct builds?
4. **Coverage completeness**: what percentage of builds have pod data, and are gaps explainable?
5. **Metric usability**: can the derived data actually answer the V2.1 target questions?

## 2. Prerequisites

Before executing this validation:
- `sync-pods` has run at least 24 hours of recurring production data
- `ci_l1_builds` is up to date (no significant lag vs pod events)
- access to Cloud Logging for cross-reference queries
- access to sampled pod metadata and matched build rows for spot-check sampling
- access to `kubectl` on the prow cluster for spot-check sampling when needed

## 3. Validation Cases

### Group V1: Source Fidelity

| ID | Case | Steps | Pass Criteria |
|---|---|---|---|
| `V1-01` | Event completeness vs Cloud Logging | 1. Pick a 1-hour window<br>2. Query Cloud Logging directly for pod events in that window<br>3. Query `ci_l1_pod_events` for the same window<br>4. Compare counts by reason | Row count matches within 5% (tolerance for edge-of-window timing) |
| `V1-02` | No data loss for FailedScheduling | 1. Query Cloud Logging for FailedScheduling events in last 24h<br>2. Query `ci_l1_pod_events` for same<br>3. Compare distinct pod counts | All FailedScheduling pods in source appear in target |
| `V1-03` | Event timestamp accuracy | 1. Sample 10 events from `ci_l1_pod_events`<br>2. Look up same events in Cloud Logging by `source_insert_id`<br>3. Compare `event_timestamp` and `receive_timestamp` | Timestamps match exactly (no timezone or truncation errors) |
| `V1-04` | Event message preservation | 1. Sample 5 Pulled events<br>2. Verify `event_message` contains image name and pull duration<br>3. Sample 3 FailedScheduling events<br>4. Verify `event_message` contains scheduling reason | Messages are preserved and parseable |
| `V1-05` | Namespace coverage | 1. List distinct `namespace_name` in `ci_l1_pod_events`<br>2. Compare against configured `POD_EVENT_NAMESPACES`<br>3. Verify no unexpected namespaces | Only expected CI namespaces present |

### Group V2: Lifecycle Derivation Correctness

| ID | Case | Steps | Pass Criteria |
|---|---|---|---|
| `V2-01` | Scheduled timestamp derivation | 1. Sample 10 pods from `ci_l1_pod_lifecycle`<br>2. For each, query raw events: `SELECT MIN(event_timestamp) WHERE event_reason='Scheduled' AND pod_name=X`<br>3. Compare with `scheduled_at` | All 10 match exactly |
| `V2-02` | First started timestamp derivation | 1. Same 10 pods<br>2. Query raw events: `SELECT MIN(event_timestamp) WHERE event_reason='Started' AND pod_name=X`<br>3. Compare with `first_started_at` | All 10 match exactly |
| `V2-03` | schedule_to_started_seconds correctness | 1. Same 10 pods<br>2. Manually compute: `first_started_at - scheduled_at` in seconds<br>3. Compare with `schedule_to_started_seconds` | All 10 match exactly (or both are NULL when either timestamp is missing) |
| `V2-04` | FailedScheduling count accuracy | 1. Sample 5 pods that have `failed_scheduling_count > 0`<br>2. Count raw events: `SELECT COUNT(*) WHERE event_reason='FailedScheduling' AND pod_name=X`<br>3. Compare | All 5 match exactly |
| `V2-05` | Pulling/Pulled timestamp derivation | 1. Sample 5 pods<br>2. Query raw events for Pulling and Pulled timestamps<br>3. Compare with `first_pulling_at` and `first_pulled_at` | All 5 match exactly |
| `V2-06` | Lifecycle row existence for all event-bearing pods | 1. Count distinct `(pod_name, pod_uid)` in `ci_l1_pod_events`<br>2. Count rows in `ci_l1_pod_lifecycle`<br>3. Find pods with events but no lifecycle row | Zero pods with events but missing lifecycle (or explained by NULL pod identity) |
| `V2-07` | Duration sanity check | 1. Query: `SELECT * FROM ci_l1_pod_lifecycle WHERE schedule_to_started_seconds < 0`<br>2. Query: `SELECT * FROM ci_l1_pod_lifecycle WHERE schedule_to_started_seconds > 3600` | Zero negative values; outliers > 1 hour are explainable (e.g., FailedScheduling retries) |

### Group V3: Jenkins Parsing And Build Linkage Quality

| ID | Case | Steps | Pass Criteria |
|---|---|---|---|
| `V3-01` | Jenkins parsing coverage | 1. Limit scope to `build_system='JENKINS'`<br>2. Compare recent Jenkins pod rows vs non-null `jenkins_build_url_key` | Coverage ≥ 80% (remaining gaps are explainable) |
| `V3-02` | Jenkins parsing correctness | 1. Sample 10 recent GCP Jenkins pod rows<br>2. Compare `jenkins_build_url_key` with the matched build URL path | All 10 are correct |
| `V3-03` | Overall linkage coverage rate | 1. Compute linkage coverage grouped by `build_system`<br>2. Compare Prow-native and Jenkins separately | Coverage ≥ 80% per build system (remaining gaps are explainable) |
| `V3-04` | Linkage correctness spot-check | 1. Sample 10 linked lifecycle rows<br>2. For each, verify direct `ci_l1_builds.pod_name` match or parsed `jenkins_build_url_key` match<br>3. Verify `repo_full_name` and `job_name` are consistent between lifecycle and build | All 10 are correct |
| `V3-05` | Parsed key ambiguity check | 1. `SELECT jenkins_build_url_key, COUNT(DISTINCT source_prow_job_id) ...`<br>2. For any ambiguities, inspect build rows | Zero true ambiguities (or collision rate < 0.1% and explained) |
| `V3-06` | Unlinked pod analysis | 1. Query lifecycle rows where `source_prow_job_id IS NULL`<br>2. Sample 10 unlinked pods<br>3. Check direct build match, then metadata-derived Jenkins key availability<br>4. Identify reason for non-linkage | Reasons are documented: timing lag, missing metadata, parse failure, non-build pod, or namespace mismatch |
| `V3-07` | Reverse linkage: builds without pod data | 1. Count recent builds in Prow-native and Jenkins scopes<br>2. Count how many have matching pod lifecycle rows<br>3. Compute reverse coverage per build system | Reverse coverage ≥ 70% for recent 24h window (gaps explained by retention or event lag) |
| `V3-08` | One-to-many preservation | 1. Sample 3 Jenkins builds known to create multiple pods<br>2. Verify multiple lifecycle rows share the same build linkage<br>3. Confirm no premature collapse to one row | Fan-out is preserved exactly as separate pod rows |

### Group V4: Metric Usability

| ID | Case | Steps | Pass Criteria |
|---|---|---|---|
| `V4-01` | Schedule wait distribution is queryable | 1. Run: `SELECT schedule_to_started_seconds, COUNT(*) FROM ci_l1_pod_lifecycle WHERE schedule_to_started_seconds IS NOT NULL GROUP BY schedule_to_started_seconds ORDER BY schedule_to_started_seconds`<br>2. Verify non-trivial distribution | At least 50 rows with non-null values; distribution shows variance (not all identical) |
| `V4-02` | FailedScheduling reason extraction | 1. Query: `SELECT event_message FROM ci_l1_pod_events WHERE event_reason='FailedScheduling' LIMIT 10`<br>2. Verify messages contain parseable scheduling failure reasons (e.g., "Insufficient cpu", "Insufficient memory") | At least 80% of messages contain identifiable reason keywords |
| `V4-03` | Image pull duration extractable | 1. For pods with both Pulling and Pulled events, compute `Pulled.event_timestamp - Pulling.event_timestamp`<br>2. Verify positive durations<br>3. Check if image name is extractable from Pulled message | Positive durations for ≥ 90% of Pulling/Pulled pairs; image name parseable |
| `V4-04` | Per-job pod overhead ranking | 1. Run: `SELECT job_name, AVG(schedule_to_started_seconds) AS avg_overhead, COUNT(*) AS cnt FROM ci_l1_pod_lifecycle WHERE schedule_to_started_seconds IS NOT NULL AND job_name IS NOT NULL GROUP BY job_name HAVING cnt >= 5 ORDER BY avg_overhead DESC LIMIT 10`<br>2. Verify results are meaningful | Returns at least 5 jobs with distinguishable overhead values |
| `V4-05` | Build-side cloud phase comparison feasibility | 1. Run: `SELECT b.cloud_phase, AVG(l.schedule_to_started_seconds), COUNT(*) FROM ci_l1_pod_lifecycle l JOIN ci_l1_builds b ON b.source_prow_job_id = l.source_prow_job_id WHERE l.schedule_to_started_seconds IS NOT NULL GROUP BY b.cloud_phase`<br>2. Verify both GCP and IDC have data | Both build cloud phases have ≥ 10 rows with non-null timing |
| `V4-06` | Time-of-day pattern feasibility | 1. Run: `SELECT HOUR(scheduled_at) AS hour_utc, AVG(schedule_to_started_seconds), COUNT(*) FROM ci_l1_pod_lifecycle WHERE schedule_to_started_seconds IS NOT NULL AND scheduled_at IS NOT NULL GROUP BY HOUR(scheduled_at) ORDER BY hour_utc`<br>2. Verify coverage across hours | At least 12 distinct hours have data |

## 4. Execution Steps

### Step 1: Confirm prerequisites

- [ ] `sync-pods` has been running ≥ 24 hours
- [ ] `ci_l1_builds` is current (lag < 2 hours)
- [ ] Cloud Logging access available for cross-reference
- [ ] sampled pod metadata and matched build rows are queryable
- [ ] kubectl access to prow cluster available

### Step 2: Run Group V1 (Source Fidelity)

Execute V1-01 through V1-05 in order. Record results.

If V1-01 shows > 5% discrepancy, investigate before proceeding.

### Step 3: Run Group V2 (Derivation Correctness)

Execute V2-01 through V2-07. Use the same sample pods across cases where possible.

If any derivation case fails, fix the bug before proceeding to linkage validation.

### Step 4: Run Group V3 (Jenkins Parsing And Build Linkage)

Execute V3-01 through V3-08.

Document coverage rates and gap reasons.

### Step 5: Run Group V4 (Metric Usability)

Execute V4-01 through V4-06.

These cases validate that the data is rich enough to power the planned dashboard charts.

### Step 6: Write validation report

Produce a summary with:
- pass/fail table by case ID
- coverage metrics (linkage rate, null rate for key fields)
- sample evidence for any failures
- recommendations (proceed / fix first / acceptable with caveats)

## 5. Validation Queries

### Q1: Source completeness cross-check (V1-01)

Against Cloud Logging (via gcloud or API):
```bash
gcloud logging read \
  'logName="projects/pingcap-testing-account/logs/events" AND resource.type="k8s_pod" AND resource.labels.namespace_name="prow-test-pods" AND timestamp>="2026-04-21T00:00:00Z" AND timestamp<"2026-04-21T01:00:00Z" AND (jsonPayload.reason="Scheduled" OR jsonPayload.reason="Pulling" OR jsonPayload.reason="Pulled" OR jsonPayload.reason="Created" OR jsonPayload.reason="Started" OR jsonPayload.reason="FailedScheduling")' \
  --format="value(insertId)" --project=pingcap-testing-account | wc -l
```

Against database:
```sql
SELECT COUNT(*) AS db_count
FROM ci_l1_pod_events
WHERE namespace_name = 'prow-test-pods'
  AND event_timestamp >= '2026-04-21 00:00:00'
  AND event_timestamp < '2026-04-21 01:00:00';
```

### Q2: Lifecycle vs raw event cross-check (V2-01, V2-02)

```sql
-- Pick sample pods
SELECT pod_name, scheduled_at, first_started_at, schedule_to_started_seconds
FROM ci_l1_pod_lifecycle
WHERE scheduled_at IS NOT NULL AND first_started_at IS NOT NULL
ORDER BY scheduled_at DESC
LIMIT 10;

-- For each sampled pod, verify against raw events
SELECT
  pod_name,
  MIN(CASE WHEN event_reason = 'Scheduled' THEN event_timestamp END) AS raw_scheduled_at,
  MIN(CASE WHEN event_reason = 'Started' THEN event_timestamp END) AS raw_first_started_at
FROM ci_l1_pod_events
WHERE pod_name IN (<sampled_pod_names>)
GROUP BY pod_name;
```

### Q3: Jenkins parsing coverage (V3-01)

```sql
SELECT
  COUNT(*) AS jenkins_pod_rows,
  SUM(CASE WHEN jenkins_build_url_key IS NOT NULL THEN 1 ELSE 0 END) AS parsed_rows,
  ROUND(SUM(CASE WHEN jenkins_build_url_key IS NOT NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS parse_pct
FROM ci_l1_pod_lifecycle
WHERE build_system = 'JENKINS';
```

### Q4: Reverse coverage — recent builds with pod data (V3-07)

```sql
SELECT
  b.build_system,
  COUNT(*) AS recent_builds_in_scope,
  SUM(CASE WHEN l.id IS NOT NULL THEN 1 ELSE 0 END) AS builds_with_pod_lifecycle,
  ROUND(SUM(CASE WHEN l.id IS NOT NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS reverse_coverage_pct
FROM ci_l1_builds b
LEFT JOIN ci_l1_pod_lifecycle l
  ON l.source_prow_job_id = b.source_prow_job_id
WHERE b.start_time >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
GROUP BY b.build_system;
```

### Q5: Duration sanity (V2-07)

```sql
-- Negative durations (should be zero)
SELECT COUNT(*) AS negative_duration_count
FROM ci_l1_pod_lifecycle
WHERE schedule_to_started_seconds < 0;

-- Extreme outliers
SELECT pod_name, job_name, schedule_to_started_seconds, failed_scheduling_count, scheduled_at
FROM ci_l1_pod_lifecycle
WHERE schedule_to_started_seconds > 3600
ORDER BY schedule_to_started_seconds DESC
LIMIT 20;
```

### Q6: FailedScheduling reason parsing (V4-02)

```sql
SELECT
  event_message,
  event_timestamp,
  pod_name
FROM ci_l1_pod_events
WHERE event_reason = 'FailedScheduling'
ORDER BY event_timestamp DESC
LIMIT 10;
```

### Q7: Image pull duration (V4-03)

```sql
SELECT
  pulling.pod_name,
  pulling.event_timestamp AS pulling_at,
  pulled.event_timestamp AS pulled_at,
  TIMESTAMPDIFF(SECOND, pulling.event_timestamp, pulled.event_timestamp) AS pull_seconds,
  pulled.event_message AS pulled_message
FROM ci_l1_pod_events pulling
JOIN ci_l1_pod_events pulled
  ON pulled.pod_name = pulling.pod_name
  AND pulled.event_reason = 'Pulled'
  AND pulled.event_timestamp >= pulling.event_timestamp
  AND pulled.event_timestamp <= DATE_ADD(pulling.event_timestamp, INTERVAL 600 SECOND)
WHERE pulling.event_reason = 'Pulling'
ORDER BY pulling.event_timestamp DESC
LIMIT 20;
```

### Q8: Per-job overhead ranking (V4-04)

```sql
SELECT
  job_name,
  COUNT(*) AS pod_count,
  AVG(schedule_to_started_seconds) AS avg_schedule_to_started_s,
  MAX(schedule_to_started_seconds) AS max_schedule_to_started_s,
  AVG(failed_scheduling_count) AS avg_failed_scheduling_count
FROM ci_l1_pod_lifecycle
WHERE schedule_to_started_seconds IS NOT NULL
  AND job_name IS NOT NULL
GROUP BY job_name
HAVING COUNT(*) >= 5
ORDER BY avg_schedule_to_started_s DESC
LIMIT 10;
```

## 6. Result Judgment Criteria

### Overall Pass

All of the following must be true:
- Group V1: all cases pass (source data is trustworthy)
- Group V2: all cases pass (derivation logic is correct)
- Group V3: V3-01 coverage ≥ 80%, V3-02 all correct, V3-05 ambiguity rate < 0.1%
- Group V4: at least 4 of 6 cases pass (data is usable for dashboard)

### Pass with Caveats

Acceptable if:
- Group V1/V2 all pass
- Group V3 coverage is 60-80% with documented gap reasons
- Group V4 has 3-4 passing cases

Caveats must be documented and tracked for resolution.

### Fail — Block Dashboard Work

If any of:
- Group V1 shows > 10% data loss
- Group V2 shows derivation bugs
- Group V3 coverage < 60%
- Group V4 has fewer than 3 passing cases

Action: fix issues before proceeding to API/chart work.

## 7. Validation Report Template

```markdown
# V2 Pod Data Validation Report

Date: YYYY-MM-DD
Data window: [start] to [end]
sync-pods running since: [date]

## Summary

| Verdict | Details |
|---|---|
| Overall | PASS / PASS WITH CAVEATS / FAIL |
| Source fidelity | X/5 pass |
| Derivation correctness | X/7 pass |
| Runtime + linkage | coverage: X%, collision: X% |
| Metric usability | X/6 pass |

## Case Results

| ID | Result | Notes |
|---|---|---|
| V1-01 | PASS/FAIL | ... |
| ... | ... | ... |

## Coverage Metrics

- Lifecycle rows: N
- Linked to builds: N (X%)
- Reverse coverage (24h): X%
- Null rate for schedule_to_started_seconds: X%
- Null rate for scheduled_at: X%

## Findings

### Issues Found
- ...

### Acceptable Gaps
- ...

## Recommendation

[ ] Safe to proceed to API/chart work
[ ] Proceed with caveats: ...
[ ] Block: fix required before proceeding
```
