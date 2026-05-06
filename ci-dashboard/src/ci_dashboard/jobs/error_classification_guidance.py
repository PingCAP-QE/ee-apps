from __future__ import annotations

ERROR_CLASSIFICATION_GUIDANCE = """Decision guidance:
- Classify the semantic root cause, not secondary Jenkins/pod noise.
- If a real test/build failure appears before Jenkins remoting, agent disconnect, post-step, or pod-event noise, prefer the test/build category.
- Failed pipeline node log excerpts are higher-signal than the root console tail when matrix branches are interrupted.
- In this autoscaled CI environment, FailedScheduling/Unschedulable/Insufficient resources/taints/PVC waits during pod Pending are normal scheduling pressure, not an error by themselves.
- Only classify Kubernetes as INFRA when there is final pod/container failure evidence such as TerminationByKubelet, OOMKilled, or Evicted.
- Container termination reason OOMKilled is INFRA/OOMKILLED, even when Jenkins later reports a matrix branch failure or agent disconnect.
- Use job names as strong hints for test families: ghpr/pull unit/mysql jobs are UT, integration/realcluster/lightning/br/dm integration jobs are IT.
- For integration job families, Jenkins/test wrapper timeout evidence such as "Timeout has been exceeded" is IT/TIMEOUT, not INFRA/NETWORK.
- BR integration matrix TEST_GROUP failures are IT/TEST_FAILURE, even if the failed case logs local 127.0.0.1 PD/TiKV connection-refused or timeout symptoms.
- Jenkins Groovy/runtime/cache/websocket/controller persistence errors are INFRA subcategories, not product BUILD failures.
- Disk-full evidence such as "No space left on device" is INFRA/DISK_FULL even when it appears while Jenkins saves pipeline state.
- Prow superseded aborts are OTHERS/SUPERSEDED_BY_NEWER_BUILD when Prow marks the job aborted because a newer same-PR same-job version is running, or when an admin-abort log has same-PR same-job newer different-SHA build evidence; this overrides downstream noise.
- Admin aborts are OTHERS/ABORT_BY_ADMIN and override downstream matrix, network, or interrupted-process symptoms.
- Merge conflicts are OTHERS, because they are neither infra nor product test/build quality failures.
- Return the default classification when the log tail is weak or only contains ambiguous downstream symptoms."""
