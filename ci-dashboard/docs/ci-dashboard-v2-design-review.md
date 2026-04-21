# CI Dashboard V2 Design Review — Cluster Verification Findings

Date: 2026-04-21

Reviewer: AI (with live cluster + DB access)

Scope: Compare V2 design docs against actual cluster state and data

Resolution status: Accepted on 2026-04-21. The V2.1 primary linkage path is now
`Cloud Logging pod event -> Kubernetes pod metadata fetch -> labels/annotations-derived build key -> ci_l1_builds match inside sync-pods`.
Jenkins console lookup and Jenkins API are no longer part of the primary V2.1
linkage path. `pod_name` may still be used to fetch the pod object, but it is
not accepted as the authoritative Jenkins build linkage signal.

## 1. Verification Method

- Connected to GKE `prow` cluster (us-central1-c, 92 nodes)
- Queried Cloud Logging for pod events in `prow-test-pods`, `jenkins-tidb`, `jenkins-tiflow`
- Queried production TiDB (`insight` DB) for `ci_l1_builds`, `ci_l1_pod_events`, `ci_l1_pod_lifecycle`
- Inspected live Jenkins agent pods via kubectl
- Cross-referenced pod names, labels, annotations, and build URLs

## 2. Confirmed Correct

| Item | Status | Evidence |
|---|---|---|
| Cloud Logging as primary source | ✅ | Events present for all CI namespaces |
| Event reasons (Scheduled/Pulling/Pulled/Created/Started/FailedScheduling) | ✅ | All observed in both prow-test-pods and jenkins-tidb |
| Two-layer pod architecture for Jenkins builds | ✅ | `ci_l1_builds.pod_name` is prow trigger pod UUID, not Jenkins agent pod |
| `prow.k8s.io/id` label on prow pods | ✅ | All prow-test-pods have it |
| Pod conditions available on pod objects (~7 day retention) | ✅ | Completed pods from 7 days ago still exist |
| Events TTL ~1 hour | ✅ | Oldest event was 67 minutes old |
| GMP + kube-state-metrics deployed | ✅ | `gmp-system` running, `kube_pod_status_unschedulable` available |
| `kubernetes.io/pod/latencies/pod_first_ready` metric available | ✅ | 3 months of history, per-pod granularity |
| Namespace override mechanism needed | ✅ | Build namespace is `apps`, pod events are in `prow-test-pods` |

## 3. Issues Found

### Issue 1: Jenkins Linkage — Simpler Path Available

**Original design under review** (FR-02): Resolve Jenkins agent pod identity by calling Jenkins API or parsing `consoleText`.

**Verified alternative**: Jenkins agent pod annotations and labels already encode the linkage key directly:

```
Pod annotation buildUrl: http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1424/
Jenkins label: pingcap_tidb_ghpr_unit_test_1424-xxx
Build URL:    https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1424/display/redirect
```

Linkage path:
1. Cloud Logging pod event → extract `pod_name` + `namespace_name`
2. Use `pod_name + namespace_name` to fetch the pod object from Kubernetes API
3. Resolve build key from `annotations.buildUrl` / `annotations.runUrl`, or from `annotations.ci_job` + `labels.jenkins/label`
3. Match to `ci_l1_builds.url` which contains `/job/{org}/job/{repo}/job/{job_name}/{build_number}/`

**Impact**: `ci_l1_build_runtime_identity` table and `ci-sync-jenkins-runtime` job may be unnecessary. The linkage can happen inside `ci-sync-pods` itself after fetching pod metadata for touched Jenkins pods.

**Advantages over consoleText approach**:
- No dependency on Jenkins API availability
- No need to fetch/parse console logs
- Works purely from Cloud Logging data already being ingested
- Simpler, fewer moving parts

**Resolution**: For V2.1, labels/annotations-based linkage is accepted as the primary linkage method.
We still allow `ci_job + jenkins/label` as an explicit metadata fallback when
`buildUrl` or `runUrl` is missing, but `pod_name` itself is no longer treated as
build linkage evidence.

### Issue 2: One-to-Many Relationship Not Addressed

**Finding**: A single Jenkins build can spawn **multiple** agent pods.

Observed example:
- `pull_integration_realcluster_test_next_gen` build 1011 → 9 pods
- `pull_integration_realcluster_test_next_gen` build 1012 → 15 pods

**Current design assumes**: 1 build → 1 pod lifecycle row.

**Impact on data model**:
- `ci_l1_pod_lifecycle` will have multiple rows per build
- `ci_l1_build_runtime_identity.runtime_pod_name` cannot hold a single value
- Aggregation strategy needed: max scheduling delay? sum of all pod prep time? worst-case pod?

**Decision needed**:
- Option A: Store every agent pod as a separate lifecycle row, aggregate at query time
- Option B: Store one aggregated lifecycle row per build (e.g., max scheduling delay across all pods)
- Option C: Store all pods, but mark one as "primary" (e.g., longest-running or last-to-start)

### Issue 3: Namespace Coverage Incomplete

**Current `sync-pods` behavior**: Collects from `prow-test-pods` only (hardcoded fallback when build namespace is `apps`).

**Verified CI namespaces with pod events**:

| Namespace | Pod Count | Activity Level | In sync-pods? |
|---|---|---|---|
| `prow-test-pods` | 126 | High (prow-native builds) | ✅ Yes |
| `jenkins-tidb` | 113 | High (tidb Jenkins builds) | ❌ No |
| `jenkins-tiflow` | 17 | Medium (tiflow Jenkins builds) | ❌ No |
| `jenkins-agents` | 1 | Low | ❌ No |

**Impact**: Without `jenkins-tidb`/`jenkins-tiflow`, ~70% of build volume (Jenkins GCP) has no pod data.

**Recommendation**: Add `jenkins-tidb,jenkins-tiflow` to `CI_DASHBOARD_POD_EVENT_NAMESPACES` default or documentation.

### Issue 4: `cloud_phase` Removed From `ci_l1_pod_lifecycle`

Migration `008_alter_ci_l1_pod_lifecycle_drop_cloud_phase.sql` removed `cloud_phase`.

**Problem**: For Jenkins pods, we need to distinguish build system type (PROW_NATIVE vs JENKINS) to answer questions like "is GCP pod-ready latency better for prow-native vs Jenkins builds?"

**Current workaround**: Join back to `ci_l1_builds` for `cloud_phase`. But if linkage depends on metadata-derived Jenkins keys rather than a direct `pod_name` match, the join path is more complex.

**Recommendation**: Consider adding a `build_system` or `pod_source_type` column to `ci_l1_pod_lifecycle` (values: `prow_native`, `jenkins_agent`) to enable direct filtering without joins.

### Issue 5: Validation Coverage Expectations Need Adjustment

**Validation plan** (V3-01): Expects linkage coverage ≥ 80%.

**Reality with current scope** (prow-test-pods only):
- Prow-native builds: ~2521/week (23% of total)
- Jenkins GCP builds: ~7492/week (70% of total)
- If only prow-test-pods is collected, max achievable coverage is ~23%

**Recommendation**: Either:
- Lower coverage threshold for Phase 1 (target: ≥ 80% of prow-native builds only)
- Or include jenkins namespaces in Phase 1 and keep the 80% target

### Issue 6: FailedScheduling Is a Major Signal for Jenkins Pods

**Finding**: `jenkins-tidb` has frequent FailedScheduling events with detailed reasons:
- "0/59 nodes are available"
- "Insufficient cpu"
- "node affinity/selector mismatch"
- "untolerated taint {ToBeDeletedByClusterAutoscaler}"

This is arguably the **highest-value signal** for V2.1 — Jenkins builds are waiting for resources more than prow-native builds.

**Recommendation**: Prioritize Jenkins namespace collection. The scheduling delay data for Jenkins pods directly answers "why are Jenkins builds slow to start?"

## 4. Decisions Made

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Jenkins linkage method | **B — Labels/annotations fetched from Kubernetes API for touched pods** | Simpler than Jenkins API, safer than pod-name guessing, and still avoids consoleText parsing |
| 2 | One-to-many (build → pods) | **A — Store all pods, aggregate at query time** | Most flexible; see Section 4.1 for aggregation design |
| 3 | Phase 1 namespace scope | **B — Include ALL CI namespaces** | Must cover all CI design namespaces to be representative |
| 4 | `ci_l1_build_runtime_identity` table | **B — Remove; do linkage inline in sync-pods** | Pod-name parsing makes the sidecar table unnecessary |
| 5 | Validation coverage target | **B — 80% per build system type** | Measure prow-native and Jenkins separately |

### 4.1 One-to-Many Aggregation Design

**Storage**: Store every Jenkins agent pod as a separate `ci_l1_pod_lifecycle` row. One build may have 1–15+ lifecycle rows.

**Linkage fields on each row**:
- `jenkins_build_url_key` — normalized URL path extracted from pod annotations or metadata fallback (e.g., `/pingcap/tidb/ghpr_unit_test/1424`)
- `source_prow_job_id` — resolved by matching `jenkins_build_url_key` to `ci_l1_builds.url`

**Query-time aggregation** (for dashboard charts):
- **Scheduling delay per build**: `MAX(schedule_to_started_seconds)` across all pods — represents the worst-case wait before the build can fully proceed
- **Total pod prep time per build**: `MAX(first_started_at) - MIN(scheduled_at)` — time from first pod scheduled to last pod started
- **FailedScheduling per build**: `SUM(failed_scheduling_count)` across all pods
- **Pod count per build**: `COUNT(*)` — useful as a complexity/parallelism indicator

**Why MAX for scheduling delay**: If a build spawns 10 pods and 9 start in 5s but 1 waits 120s for scheduling, the build is blocked until that last pod is ready. The bottleneck pod determines the real impact.

**Index support**: Add index on `(jenkins_build_url_key)` to `ci_l1_pod_lifecycle` for efficient per-build aggregation.

## 5. Required Document Updates (Confirmed)

Based on decisions above:

1. **ci-dashboard-v2-design.md**:
   - Remove FR-02 (Jenkins runtime identity via consoleText/API) — replace with pod-metadata-based linkage description
   - Remove `ci_l1_build_runtime_identity` table from data model
   - Remove `ci-sync-jenkins-runtime` job from job design
   - Add one-to-many relationship handling (store all pods, aggregate at query)
   - Add `jenkins_build_url_key` to `ci_l1_pod_lifecycle` schema
   - Update architecture diagram: remove `ci_l1_build_runtime_identity`, show jenkins namespaces flowing into `ci-sync-pods`
   - Update namespace scope to explicitly list all CI namespaces

2. **ci-dashboard-v2-implementation.md**:
   - Update Phase A: include jenkins namespace support and pod metadata fetch
   - Remove Phase references to `ci-sync-jenkins-runtime`
   - Add labels/annotations → build URL key resolution logic to sync-pods code plan

3. **ci-dashboard-v2-pod-validation-plan.md**:
   - Split linkage coverage metrics by build system (prow-native vs Jenkins)
   - Add Jenkins-specific validation cases (one-to-many, metadata-linkage accuracy)
   - Adjust coverage thresholds per build system

4. **ci-dashboard-v2-sync-pods-readiness.md**:
   - Update namespace scope: `prow-test-pods,jenkins-tidb,jenkins-tiflow`
   - Add Jenkins pod metadata access and linkage as readiness gates
   - Document one-to-many behavior

5. **ci-dashboard-v2-pod-question-map.md**:
   - Remove references to `ci_l1_build_runtime_identity`
   - Note that all questions now cover both prow-native and Jenkins builds in Phase 1
