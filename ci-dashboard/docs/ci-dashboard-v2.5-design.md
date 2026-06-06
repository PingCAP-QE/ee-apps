# CI Dashboard V2.5 Design

Status: Draft v0.3

Last updated: 2026-04-24

Reference inputs:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2.5/ci-dashboard/docs/ci-dashboard-v2-design.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2.5/ci-dashboard/docs/ci-dashboard-v2-implementation.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2.5/ci-dashboard/docs/validation-artifacts/2026-04-24/validation-report.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2.5/ci-dashboard/docs/validation-artifacts/2026-04-24/baseline-counts.md`

## 1. Why V2.5 Exists

V2.1 proved that the pod-event ingestion path is production-viable:
- `sync-pods` is fresh
- raw event import is idempotent
- event volume and event reasons are operationally meaningful

But the 2026-04-24 validation round also proved that the current V2.1 linkage model is not structurally sufficient for production-quality dashboard use.

Two design problems were observed:

1. **Linkage is computed too early and too permanently**
- pod lifecycle rows are derived before build rows have necessarily arrived
- linkage is attempted once during `sync-pods`
- when the build arrives later, many lifecycle rows remain stale and unlinked

2. **Jenkins pod metadata depends on live lookup**
- Jenkins linkage depends on labels and annotations
- current V2.1 fetches that metadata by listing live pods during `sync-pods`
- short-lived pods disappear before that lookup can happen
- metadata capture therefore decays sharply with pod age

These are not tuning problems. They are model problems.

V2.5 exists to correct the model rather than adding another temporary patch.

## 2. V2.5 Objectives

V2.5 must:
- make pod-to-build linkage **eventually correct**, not just opportunistically correct at first ingest
- treat multiple linkage inputs as **formal evidence sources**, not primary-vs-fallback hacks
- preserve one lifecycle row per pod and one build row per build
- support deterministic recomputation when upstream tables arrive out of order
- keep existing V1 and V2 APIs stable while improving data quality underneath

## 3. Non-Goals

V2.5 does not aim to:
- redesign dashboard UI or charts yet
- replace Cloud Logging as the primary source for pod lifecycle events
- introduce Kafka unless a broader platform requirement appears
- solve all pod-stage timing fields in the same slice
- require a dedicated long-running metadata watcher in the first delivery slice if equivalent linkage quality can be reached without it

Kafka may become useful later if multiple downstream consumers need the same pod identity stream, but it is not the primary V2.5 requirement.

## 4. Core Design Decisions

### 4.1 Linkage Uses Multiple Formal Evidence Sources

V2.5 explicitly treats the following as first-class evidence:

1. **Direct Prow pod identity**
- `ci_l1_pod_lifecycle.pod_name` -> `ci_l1_builds.pod_name`

2. **Kubernetes metadata-derived Jenkins identity**
- labels and annotations from Kubernetes API
- examples:
  - `buildUrl`
  - `runUrl`
  - `ci_job`
  - `org`
  - `repo`
  - `jenkins/label`
  - `jenkins_controller`

3. **Pod-name-derived Jenkins identity**
- generic parse rule applied to `pod_name`
- resulting candidate `normalized_build_url`
- usable even after the live pod has disappeared

No single source is considered the only valid path to truth.

The stable design point is:
- each source produces auditable evidence
- reconcile chooses the best consistent match
- final linkage is determined by consistency checks, not by whichever source ran first

### 4.2 Pod-Name Parsing Uses One Generic Rule Plus A Prefix Mapping Table

V2.5 should not build a heavy parser registry unless the data later proves it necessary.

The validation result so far suggests that the large majority of parseable Jenkins pod names share one common shape:
- `{prefix}-{build_number}-{random_suffix}`

For the first V2.5 slice, the parser model should therefore stay simple:
- one generic parser that scans from right to left and extracts the first plausible build-number segment
- one prefix-to-template mapping table that turns the parsed prefix into a canonical Jenkins build URL

Examples of prefixes/families that validation suggests are covered by this shape:
- `pingcap-tidb-*`
- `pingcap-tiflow-*`
- `gcap-*`
- `xdedicated-*`
- selected `other` families with stable naming patterns

Examples that should remain unresolved in the first slice:
- `dm-it-*`
- opaque runtime pod families whose pod name does not contain a reliable build number

The design point is:
- keep the parser simple
- keep the mapping table explicit
- fail closed when no reliable build number or mapping exists

The mapping table may be seeded from already linked data and refreshed from validation artifacts, but it should remain deterministic and reviewable.

### 4.3 Linkage Becomes A Reconcile Stage

V2.5 explicitly separates:
- raw facts
- lifecycle derivation
- candidate evidence extraction
- build linkage reconciliation

This means pod/build linkage is no longer modeled as a one-time side effect during `sync-pods`.

Instead, V2.5 adds a reconcile stage that:
- repeatedly revisits recent unresolved lifecycle rows
- consumes all currently available evidence:
  - direct pod-name match
  - metadata-derived Jenkins URL evidence
  - pod-name-derived Jenkins URL evidence
- updates prior rows when build rows arrive later than pod rows
- surfaces ambiguity or mismatch in logs and validation rather than silently freezing an early null result

### 4.4 The System Must Converge Under Out-Of-Order Arrival

V2.5 treats out-of-order arrival as a normal condition.

Expected orderings include:
- pod events before build row
- build row before pod events
- metadata lookup succeeds before build row
- metadata lookup fails but pod-name parse succeeds
- metadata becomes available later than first lifecycle derivation

The correct system behavior is therefore:
- raw facts remain append/upsert oriented
- derived lifecycle rows remain recomputable
- linkage state improves monotonically as more inputs and better evidence arrive

### 4.5 Durable Metadata Capture Remains A Supported Enhancement

V2.5 does not require a dedicated watch worker in the first slice if formal evidence sources plus reconcile already deliver the needed coverage and correctness.

However, a durable metadata collector remains compatible with this design and may still be introduced later for:
- opaque pod families that cannot be parsed safely from `pod_name`
- richer auditability for labels and annotations
- future downstream consumers that need pod identity history

The key architectural point is:
- a metadata collector is an **additional evidence source**
- it is not the only correctness path

## 5. Proposed Data Model

## 5.1 Existing Tables Kept

- `ci_l1_pod_events`
- `ci_l1_pod_lifecycle`
- `ci_l1_builds`
- `ci_job_state`

## 5.2 Minimal Persisted Linkage State

V2.5 should keep the schema changes small.

V2.5 should converge on one canonical linkage field name across build rows and pod lifecycle rows:
- `normalized_build_url`

Recommended shape:
- `ci_l1_builds.url`
  - raw source URL kept for audit/debug and upstream fidelity
- `ci_l1_builds.normalized_build_url`
  - canonical build URL used for linkage and downstream joins
- `ci_l1_pod_lifecycle.normalized_build_url`
  - same canonical build URL when pod-side evidence is available

Canonical URL form should be:
- full URL, not path-only
- public canonical host
  - Jenkins should canonicalize to `https://prow.tidb.net/jenkins/...`
  - Prow-native should stay on `https://prow.tidb.net/view/gs/...`
- no trailing `/display/redirect`
- trailing slash preserved on the final build URL
- example Jenkins form:
  - `https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/1718/`

Candidate URLs from different evidence sources do not need to be persisted separately in the first slice.

The first V2.5 slice also does not need new row-level bookkeeping such as:
- per-row `linkage_method`
- per-row `linkage_last_attempted_at`

Those fields are mainly operational/debug metadata. They do not improve dashboard correctness or downstream analysis by themselves.

For this slice:
- reconcile should remain stateless at the row-schema level
- source mix, parse hit rate, and unresolved counts should be emitted through validation artifacts, summary logs, or job metrics
- if future operations need retry/backoff state, that can be added later with a clearer justification

If metadata-derived and pod-name-derived candidates disagree, that disagreement should be handled inside reconcile logic and tracked in validation summaries or logs rather than expanding every lifecycle row into a full state machine.

## 5.3 Optional Table: Pod Metadata

If we later add a dedicated collector, V2.5 can introduce:
- `ci_l1_pod_metadata`

Suggested fields:
- identity
  - `source_project`
  - `cluster_name`
  - `location`
  - `namespace_name`
  - `pod_name`
  - `pod_uid`
- raw metadata
  - `pod_labels_json`
  - `pod_annotations_json`
- extracted fields
  - `pod_author`
  - `pod_org`
  - `pod_repo`
  - `ci_job`
  - `jenkins_label`
  - `jenkins_label_digest`
  - `jenkins_controller`
  - `build_url`
  - `run_url`
  - `normalized_build_url`
- observation state
  - `first_seen_at`
  - `last_seen_at`
  - `deleted_at`
  - `last_resource_version`
- audit fields
  - `created_at`
  - `updated_at`

Notes:
- one row per current pod identity is sufficient for V2.5
- if later history proves necessary, V2.6+ can add a versioned snapshot table

## 6. Proposed Job Topology

## 6.1 `ci-sync-pods`

Responsibility:
- read Cloud Logging pod events
- normalize and upsert `ci_l1_pod_events`
- derive/recompute lifecycle timing rows in `ci_l1_pod_lifecycle`
- extract candidate linkage evidence
  - metadata-derived canonical build URL when Kubernetes metadata is available
  - pod-name-derived canonical build URL when a family parser applies

V2.5 change:
- `ci-sync-pods` may still try Kubernetes API lookup because it is high-quality evidence when the pod is alive
- but correctness no longer depends on that lookup being the only source
- `ci-sync-pods` should evaluate both evidence candidates when available, choose one effective key for the row, and emit source-level validation counters in logs or metrics

## 6.2 Reconcile Step Inside `ci-sync-pods`

Responsibility:
- revisit recent lifecycle rows, especially unresolved rows
- link Prow-native lifecycle rows by `pod_name`
- link Jenkins lifecycle rows by:
  - metadata-derived candidate URL
  - pod-name-derived candidate URL
- apply time-window and uniqueness checks when ambiguity exists
- update lifecycle rows in place when a previously missing build match becomes available

Recommended scope:
- recent unresolved window such as the last 24-72 hours
- plus limited refresh of recently linked rows if tie-break logic changes

For the first slice, this should be implemented as a **post-step inside `sync-pods`**, not as a separately scheduled job.

Reasoning:
- the core logic is still small
- it avoids another CronJob, state row, and rollout surface
- if the logic later becomes operationally heavy, it can still be split into a dedicated job without changing the data model

## 6.3 Optional Future Worker: `ci-sync-pod-metadata`

Responsibility:
- continuously collect pod labels and annotations from Kubernetes API
- persist pod identity metadata into `ci_l1_pod_metadata`
- enrich opaque families that cannot be safely resolved from `pod_name`

Runtime recommendation if enabled:
- long-running worker
- target namespaces:
  - `jenkins-tidb`
  - `jenkins-tiflow`
  - `prow-test-pods` only if later needed

This worker is compatible with V2.5, but it is not the defining architectural requirement for the first slice.

## 7. Linkage Strategy In V2.5

### 7.1 Prow-native Path

Primary linkage:
- `ci_l1_pod_lifecycle.pod_name` -> `ci_l1_builds.pod_name`

Because validation already showed many rows that are now directly matchable but remain stale, the key improvement is not a new key. The key improvement is repeated reconciliation.

### 7.2 Jenkins Path

Jenkins linkage in V2.5 uses two formal evidence sources:

1. **Metadata-derived candidate URL**
- source:
  - `buildUrl`
  - `runUrl`
  - `ci_job + jenkins_label + org + repo`
- normalized into canonical `normalized_build_url`
- preferred when available because it carries richer identity context

2. **Pod-name-derived candidate URL**
- source:
  - generic parser over `pod_name`
  - prefix-to-key-template mapping table
- normalized into canonical `normalized_build_url`
- used when metadata is absent or when it independently corroborates metadata

Neither source becomes final linkage by itself.

The stable design point is:
- each source can produce a candidate canonical build URL
- one effective `normalized_build_url` is persisted on the lifecycle row
- reconcile tests candidate uniqueness and consistency
- final linkage is chosen from the strongest consistent evidence set

### 7.3 Ambiguity Handling

Validation already found recent cases where one canonical Jenkins URL matched more than one build id.

For the first slice, ambiguity handling should stay simple:
- choose the build whose `start_time` is closest to the pod `scheduled_at`

This is sufficient for the currently observed collision rate.

If future validation shows this is not enough, additional tie-break rules can be added later.

## 8. Validation Expectations For V2.5

V2.5 is complete only if the new model changes the measured failure modes, not just the code structure.

Required validation outcomes:
- pod-name evidence covers the expected high-volume Jenkins families
- pod-name-derived linkage is auditable and correct on samples
- metadata evidence and pod-name evidence can coexist without conflict
- rows that become matchable later are actually backfilled
- `PROW_NATIVE` linkage no longer leaves large numbers of directly matchable rows unresolved
- ambiguous Jenkins matches are explicitly surfaced and bounded

New validation cases should include:
- evidence coverage by source in run summaries:
  - `K8S_METADATA`
  - `POD_NAME_PARSE`
  - `DIRECT_POD_NAME`
- parser correctness by family
- count of lifecycle rows now matchable but still unresolved
- reconcile effectiveness after delayed build arrival
- reconcile idempotency

## 9. Rollout Shape

Recommended delivery order:

1. Land the app/jobs code that reads and writes canonical `normalized_build_url`.
2. Build and publish the new images.
3. Enter a short maintenance window and pause the scheduled jobs.
4. Apply the schema cutover migration:
- rename `ci_l1_builds.normalized_build_key` -> `normalized_build_url`
- add/canonicalize `ci_l1_pod_lifecycle.normalized_build_url`
- drop old pod linkage columns after backfill into the new canonical field
5. Run one-off data refresh steps against the migrated schema:
- `backfill-range` for the target date range when build-side refresh is needed
- `reconcile-pod-linkage-range` for the target pod scheduled date range
6. Deploy the new app/jobs workloads and resume the scheduled jobs.
7. Run validation on:
- evidence coverage by source
- parser accuracy by family
- delayed-build backfill effectiveness
- ambiguity handling
- pod/query/dashboard reader correctness after the cutover
8. Decide whether an additional metadata worker is still needed for unresolved opaque families.
9. Only then continue pod-facing dashboard work.

### 9.1 Compatibility Rules

V2.5 is intentionally **not** using long-lived dual-write.

Compatibility stance:
- before the maintenance window, the currently running V1/V2 app and jobs continue unchanged
- during the maintenance window, jobs are paused while schema and data are cut over together
- after the migration, the new V2.5 app/jobs must be deployed before the scheduled jobs are resumed

This means:
- there is no long-term mixed-schema support requirement
- there is no requirement to keep `normalized_build_key` and `jenkins_build_url_key` alive after cutover
- a short downtime / data lag window is accepted in exchange for a cleaner schema and simpler logic

Impact on currently running versions:
- the existing V1/V2 workloads are unaffected until the maintenance window starts
- once the schema migration has run, old images should not keep running against the migrated schema
- the cutover therefore depends on deploy order inside the maintenance window, not on dual-write compatibility code

### 9.2 Runtime Dependency Order

V2.5 does have a data dependency chain, but it is handled inside the maintenance window instead of by a dual-write phase.

Operationally:
- V1 build ingestion remains the upstream source of truth for build rows
- V2.5 pod linkage depends on build rows existing in `ci_l1_builds`
- reconcile improves correctness after out-of-order arrival, but it still depends on build-side canonical URL population happening first

Recommended upgrade order:
1. publish the new images
2. pause scheduled jobs
3. run schema migration
4. verify canonical URL population on `ci_l1_builds` and `ci_l1_pod_lifecycle`
5. run one-off backfill / reconcile commands
6. deploy the new app/jobs workloads
7. validate linkage uplift and reader correctness
8. resume scheduled jobs

Dependency summary:
- V1/V2 do not depend on V2.5 before the cutover starts
- V2.5 still depends on the existing build pipeline staying healthy because build rows remain the upstream truth
- post-cutover readers and jobs depend on the migration plus one-off reconcile/backfill having completed first

## 10. What V2.5 Intentionally Leaves For Later

- richer pod status/spec timing fields
- Jenkins log ingestion
- broader failure classification redesign
- Kafka/event-bus adoption unless another consumer justifies it

## 11. Summary

V2.1 proved that the event stream is useful.

V2.5 exists because useful events are not enough:
- linkage must combine multiple formal evidence sources
- pod/build linkage must be convergent

The defining architectural changes for V2.5 are therefore:
- **treat metadata-derived and pod-name-derived keys as first-class evidence**
- **treat linkage as a reconcile stage, not a one-time side effect**
