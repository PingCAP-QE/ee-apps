from __future__ import annotations

ERROR_CLASSIFICATION_GUIDANCE = """Decision guidance:
- Classify the semantic root cause, not secondary Jenkins/pod noise.
- If a real test/build failure appears before Jenkins remoting, agent disconnect, post-step, or pod-event noise, prefer the test/build category.
- Failed pipeline node log excerpts are higher-signal than the root console tail when matrix branches are interrupted.
- Stage names or wrapper steps such as "prepare depends", "build", or generic pipeline/config steps are weak hints; classify the underlying compiler, dependency, test, or merge evidence instead.
- In this autoscaled CI environment, FailedScheduling/Unschedulable/Insufficient resources/taints/PVC waits during pod Pending are normal scheduling pressure, not an error by themselves.
- Only classify Kubernetes as INFRA when there is final pod/container failure evidence such as TerminationByKubelet, OOMKilled, or Evicted.
- Container termination reason OOMKilled is INFRA/OOMKILLED, even when Jenkins later reports a matrix branch failure or agent disconnect.
- Use job names as strong hints for test families: ghpr/pull unit/mysql jobs are UT, integration/realcluster/lightning/br/dm integration jobs are IT.
- ghpr_check2 test-failure evidence should usually stay in the UT family because that job behaves like a unit-style mixed check job; do not move it to IT unless the failing evidence is explicitly integration-specific.
- Go compiler errors such as "undefined:" or "has no field or method" are BUILD/COMPILE, even when they appear inside Bazel or wrapper output.
- Bazel/unit summaries such as "FAILED TO BUILD", "fails to build", or "0 failing out of 0 test cases" point to an upstream build problem, not a UT test failure by themselves.
- Failpoint rewrite/parser errors such as "Rewrite error ... expected declaration/statement, found '<<'" are BUILD/COMPILE.
- Dependency hygiene failures such as "updates to go.mod needed", "missing strict dependencies", "No dependencies were provided", or imports/deps mismatch are BUILD/DEPENDENCY.
- Nogo or build-time static-analysis validation failures such as "Validating nogo output" are UT/FORMAT_CHECK, including ghpr_build and pull_build_next_gen jobs; do not leave them in BUILD/LINT or move them to BUILD/COMPILE.
- Repository/archive download failures such as "Error downloading", "Error computing the main repository mapping", or 429/502/503 while fetching Bazel/GitHub dependencies are INFRA/EXTERNAL_DEP even if matrix branches later show test interruptions.
- For integration job families, Jenkins/test wrapper timeout evidence such as "Timeout has been exceeded" is IT/TIMEOUT, not INFRA/NETWORK.
- BR integration matrix TEST_GROUP failures are IT/TEST_FAILURE, even if the failed case logs local 127.0.0.1 PD/TiKV connection-refused or timeout symptoms.
- Jenkins Groovy/runtime/cache/websocket/controller persistence errors are INFRA subcategories, not product BUILD failures.
- Disk-full evidence such as "No space left on device" is INFRA/DISK_FULL even when it appears while Jenkins saves pipeline state.
- Prow superseded aborts are OTHERS/SUPERSEDED_BY_NEWER_BUILD when Prow marks the job aborted because a newer same-PR same-job version is running, or when an admin-abort log has same-PR same-job newer different-SHA build evidence; this overrides downstream noise.
- Admin aborts are OTHERS/ABORT_BY_ADMIN and override downstream matrix, network, or interrupted-process symptoms.
- Merge conflicts such as "CONFLICT (content)" or "Automatic merge failed" are OTHERS/CODE_CONFLICT, because they are neither infra nor product test/build quality failures.
- Return the default classification when the log tail is weak or only contains ambiguous downstream symptoms."""
