from __future__ import annotations

from ci_dashboard.jobs.rule_engine import RuleEngine


def test_rule_engine_matches_network_infra_rule() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text="dial tcp 10.10.10.10:443: i/o timeout",
        build={"job_name": "ghpr_check2", "url": "https://prow.tidb.net/job/x"},
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "NETWORK"
    assert classification.source == "rule:infra_network"


def test_rule_engine_matches_unit_test_rule_using_job_name_and_log_text() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text="--- FAIL: TestDDLBasic (0.00s)\nFAIL\n",
        build={"job_name": "ghpr_unit_test", "url": "https://prow.tidb.net/job/x"},
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "TEST_FAILURE"


def test_rule_engine_classifies_ghpr_mysql_test_diff_as_unit_test_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "./t/index_merge_mvindex_single_table.test: ok! 1005 test cases passed\n"
            'time="2026-04-30T11:30:51+08:00" level=error msg="1 tests failed\\n"\n'
            'time="2026-04-30T11:30:51+08:00" level=error '
            'msg="run test [gcol_alter_table] err: sql:alter table t modify column c int '
            "generated always as (a + 10) virtual;: failed to run query\\n"
            "but got(159):\\n"
            "Error 3106 (HY000): Unsupported modification for generated columns covered by an index\\n"
            "diff:\\n"
            'script returned exit code 1"\n'
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_mysql_test",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_mysql_test/2544/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:unit_mysql_test_failure"


def test_rule_engine_classifies_pull_mysql_test_diff_as_unit_test_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            'time="2026-04-30T11:30:51+08:00" level=error msg="1 tests failed\\n"\n'
            'time="2026-04-30T11:30:51+08:00" level=error '
            'msg="run test [gcol_alter_table] err: failed to run query\\n"\n'
            "but got(159):\\n"
            "Error 3106 (HY000): Unsupported modification for generated columns covered by an index\\n"
            "diff:\\n"
        ),
        build={
            "job_name": "pingcap/tidb/release-8.5/pull_mysql_test",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/release-8.5/job/pull_mysql_test/1/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:unit_mysql_test_failure"


def test_rule_engine_prefers_unit_bazel_failure_over_k8s_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning  FailedScheduling  default-scheduler  "
            "0/90 nodes are available: 76 Insufficient memory, "
            "1 node(s) had untolerated taint {ToBeDeletedByClusterAutoscaler: 1777537003}.\n"
            "Pod [Pending][Unschedulable] 0/90 nodes are available: "
            "76 Insufficient memory.\n"
            "FAIL: //br/pkg/restore/snap_client:snap_client_test "
            "(shard 35 of 47)\n"
            "ERROR: /workspace/tidb/br/pkg/restore/snap_client/BUILD.bazel:86:8: "
            "Testing //br/pkg/restore/snap_client:snap_client_test "
            "(shard 35 of 47) failed: Test failed, aborting\n"
            "=== RUN   TestMonitorTheSystemTableIncremental\n"
            "    systable_restore_test.go:396:\n"
            "        Error:       Not equal:\n"
            "        expected: 258\n"
            "        actual  : 259\n"
            "--- FAIL: TestMonitorTheSystemTableIncremental (0.00s)\n"
            "Test cases: finished with 6221 passing and 1 failing out of 6222 test cases\n"
            "make: *** [Makefile:705: bazel_ci_test] Error 3\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_unit_test",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2968/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:unit_bazel_test_failure"


def test_rule_engine_prefers_ghpr_check2_bazel_test_failure_over_network_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "INFO: Build completed, 1 test FAILED, 5247 total actions\n"
            "//tests/realtikvtest/importintotest:importintotest_test FAILED in 3 out of 3 in 0.3s\n"
            "Executed 1 out of 1 test: 1 fails locally.\n"
            "make: *** [Makefile:837: bazel_importintotest] Error 3\n"
            "Bazel caught terminate signal; shutting down.\n"
            "Could not interrupt server: (14) Connection reset by peer\n"
            "Server terminated abruptly (error code: 14, error message: 'Connection reset by peer')\n"
            "script returned exit code 143\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2567/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:unit_bazel_test_failure"


def test_rule_engine_classifies_ghpr_check2_nogo_failure_as_format_check() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Validating nogo output for //pkg/statistics:statistics failed\n"
            "ERROR: /home/jenkins/agent/workspace/pingcap/tidb/pkg/statistics/BUILD.bazel:3:11: "
            "GoCompilePkg pkg/statistics/statistics.a failed\n"
            "script returned exit code 1\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2645/console",
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "FORMAT_CHECK"
    assert classification.source == "rule:unit_format_check_nogo_failure"


def test_rule_engine_classifies_pull_build_next_gen_nogo_failure_as_format_check() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "ERROR: /home/jenkins/agent/workspace/pingcap/tidb/pull_build_next_gen/tidb/"
            "pkg/executor/BUILD.bazel:3:11: Validating nogo output for "
            "//pkg/executor:executor failed: (Exit 1): builder failed\n"
            "nogo: errors found by nogo during build-time code analysis:\n"
            "pkg/executor/adapter.go:1315:14: confusing-results: unnamed results of "
            "the same type may be confusing, consider using named results (revive)\n"
            "make: *** [Makefile:744: bazel_build] Error 1\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_build_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_build_next_gen/1990/console"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "FORMAT_CHECK"
    assert classification.source == "rule:unit_format_check_nogo_failure"


def test_rule_engine_classifies_ghpr_build_nogo_failure_as_format_check() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "ERROR: /home/jenkins/agent/workspace/pingcap/tidb/ghpr_build/tidb/"
            "pkg/statistics/BUILD.bazel:64:8: Validating nogo output for "
            "//pkg/statistics:statistics_test failed: (Exit 1): builder failed\n"
            "nogo: errors found by nogo during build-time code analysis:\n"
            "pkg/statistics/histogram_test.go:358:71: empty-lines: extra empty line "
            "at the start of a block (all_revive)\n"
            "make: *** [Makefile:744: bazel_build] Error 1\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_build",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2310/console",
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "FORMAT_CHECK"
    assert classification.source == "rule:unit_format_check_nogo_failure"


def test_rule_engine_prefers_code_conflict_over_pipeline_config_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "WorkflowScript.run(WorkflowScript:114)\n"
            "Pre-merge heads of pull requests to base SHA: "
            "b6200ce8d00986696ee0bba25fb4c0b9c792d993 ...\n"
            "Auto-merging pkg/resourcegroup/runaway/manager.go\n"
            "Auto-merging pkg/session/upgrade_def.go\n"
            "CONFLICT (content): Merge conflict in pkg/session/upgrade_def.go\n"
            "Automatic merge failed; fix conflicts and then commit the result.\n"
            "ERROR: script returned exit code 1\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_integration_realcluster_test_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_integration_realcluster_test_next_gen/945/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "CODE_CONFLICT"
    assert classification.source == "rule:others_code_conflict"


def test_rule_engine_prefers_disk_full_over_dm_integration_and_jenkins_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@6a644e73:JNLP4-connect]\n"
            "cd dm && ./tests/run_group.sh G11\n"
            "make: *** [Makefile:529: dm_integration_test_in_group] Terminated\n"
            "Error when executing failure post condition:\n"
            "java.io.IOException: No space left on device\n"
            "at PluginClassLoader for workflow-support//"
            "org.jenkinsci.plugins.workflow.support.pickles.serialization."
            "RiverWriter.<init>(RiverWriter.java:109)\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tiflow/pull_dm_integration_test_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/"
                "pull_dm_integration_test_next_gen/171/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "DISK_FULL"
    assert classification.source == "rule:infra_disk_full"


def test_rule_engine_prefers_dm_integration_failure_over_jenkins_remoting_warning() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@5b4ec242:JNLP4-connect]\n"
            "cd dm && ./tests/run_group.sh G00\n"
            "HTTP 127.0.0.1:8361/apis/v1alpha1/status/test is not alive\n"
            "curl: (7) Failed to connect to 127.0.0.1 port 8261: Connection refused\n"
            "make: *** [Makefile:529: dm_integration_test_in_group] Error 1\n"
            "Failed in branch Matrix - TEST_GROUP = 'G00'\n"
            "ERROR: script returned exit code 2\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tiflow/pull_dm_integration_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/"
                "pull_dm_integration_test/389/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:dm_integration_test_failure"


def test_rule_engine_classifies_cdc_storage_matrix_failure_as_integration_test_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Failed in branch Matrix - TEST_GROUP = 'G11'\n"
            "TEST FAILED: OUTPUT DOES NOT CONTAIN 'id: 1'\n"
            "check data failed 5-th time, retry later\n"
            "pingcap-tiflow-pull-cdc-integration-storage-test seems to be removed or offline "
            "(hudson.remoting.ChannelClosedException)\n"
            "script returned exit code 143\n"
        ),
        build={
            "job_name": "pingcap/tiflow/pull_cdc_integration_storage_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/"
                "pull_cdc_integration_storage_test/166/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:cdc_integration_storage_test_failure"


def test_rule_engine_prefers_oomkilled_over_cdc_storage_matrix_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "[PodInfo] jenkins-tiflow/"
            "pingcap-tiflow-pull-cdc-integration-storage-test-266-bw7p-l7b0x\n"
            "    Container [golang] terminated [OOMKilled] No message\n"
            "    Pod [Running][ContainersNotReady] containers with unready status: [golang]\n"
            "Failed in branch Matrix - TEST_GROUP = 'G11'\n"
            "script returned exit code 143\n"
            "    Container [jnlp] terminated [Error] No message\n"
            "    Pod [Failed][PodFailed] No message\n"
        ),
        build={
            "job_name": "pingcap/tiflow/pull_cdc_integration_storage_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/"
                "pull_cdc_integration_storage_test/266/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "OOMKILLED"
    assert classification.source == "rule:infra_k8s_oomkilled"


def test_rule_engine_classifies_dm_next_gen_integration_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@33ae5ad3:JNLP4-connect]\n"
            "cd dm && ./tests/run_group.sh G01\n"
            "Run cases: ha_cases3 ha_cases3_1 ha_master tracker_ignored_ddl\n"
            "Starting TiDB on port 4000\n"
            "Verifying TiDB is started...\n"
            "ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1' (111)\n"
            "invalid config: keyspace name or standby mode is required for nextgen TiDB\n"
            "Failed to start TiDB\n"
            "make: *** [Makefile:528: dm_integration_test_in_group] Error 1\n"
            "Failed in branch Matrix - TEST_GROUP = 'G01'\n"
            "ERROR: script returned exit code 2\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tiflow/pull_dm_integration_test_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/"
                "pull_dm_integration_test_next_gen/293/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:dm_integration_test_failure"


def test_rule_engine_prefers_merged_integration_test_failure_over_jenkins_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@4a8b1014:JNLP4-connect]\n"
            "2026/04/30 12:59:00 Test fail: Outputs are not matching.\n"
            "Test case: sql/randgen/6_date_2.sql\n"
            "2026/04/30 12:59:00 Test summary(sql/randgen/6_date_2.sql): Test case FAIL\n"
            "push_down_test_bin exit code is 2\n"
            "make: *** [Makefile:5: push-down-test] Error 2\n"
            "ERROR: script returned exit code 2\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/merged_integration_copr_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "merged_integration_copr_test/140/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:merged_integration_test_failure"


def test_rule_engine_prefers_br_integration_matrix_failure_over_network_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "[pd] cannot update member from this url "
            "url=https://127.0.0.1:2379 "
            "error=\"transport: Error while dialing: dial tcp 127.0.0.1:2379: "
            "connect: connection refused\"\n"
            "context deadline exceeded while waiting for local test service\n"
            "script returned exit code 143\n"
            "Failed in branch Matrix - TEST_GROUP = 'G04'\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_br_integration_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_br_integration_test/896/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:br_integration_test_failure"


def test_rule_engine_classifies_br_443_local_pd_failure_as_integration_test_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "[pd] cannot update member from this url "
            "url=https://127.0.0.1:2379 "
            "error=\"transport: Error while dialing: dial tcp 127.0.0.1:2379: "
            "connect: connection refused\"\n"
            "failed to delete region label rule, the rule will be removed after ttl expires "
            "error=\"context deadline exceeded\"\n"
            "script returned exit code 143\n"
            "Failed in branch Matrix - TEST_GROUP = 'G04'\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_br_integration_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_br_integration_test/443/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:br_integration_test_failure"


def test_rule_engine_classifies_br_integration_timeout_before_matrix_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Cancelling nested steps due to timeout\n"
            "Sending interrupt signal to process\n"
            "error=\"transport: Error while dialing: dial tcp 127.0.0.1:2379: "
            "connect: connection refused\"\n"
            "script returned exit code 143\n"
            "Failed in branch Matrix - TEST_GROUP = 'G08'\n"
            "End of Pipeline\n"
            "Timeout has been exceeded\n"
            "Finished: ABORTED\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_br_integration_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_br_integration_test/966/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TIMEOUT"
    assert classification.source == "rule:integration_test_timeout"


def test_rule_engine_prefers_kubelet_eviction_over_jenkins_disconnect_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Pod [Failed][TerminationByKubelet] The node was low on resource: memory. "
            "Threshold quantity: 100Mi, available: 48144Ki. "
            "Container golang was using 15109Mi, request is 8Gi, "
            "has larger consumption of memory. "
            "Container jnlp was using 582800Ki, request is 256Mi, "
            "has larger consumption of memory.\n"
            "Pod [Failed][PodFailed] No message\n"
            "hudson.remoting.ChannelClosedException: channel is already closed\n"
            "Agent went offline during the build\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2548/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "K8S_MEMORY_EVICTION"
    assert classification.source == "rule:infra_k8s_kubelet_eviction"


def test_rule_engine_prefers_final_k8s_failure_over_jenkins_disconnect_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Pod [Failed][Evicted] The node was low on resource: ephemeral-storage.\n"
            "Container [golang] terminated [Evicted] No message\n"
            "hudson.remoting.ChannelClosedException: channel is already closed\n"
            "Agent went offline during the build\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2602/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "K8S"
    assert classification.source == "rule:infra_k8s_runtime"


def test_rule_engine_prefers_jenkins_groovy_over_remoting_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@197337ba:JNLP4-connect]\n"
            "Also: org.jenkinsci.plugins.workflow.actions.ErrorAction$ErrorId: "
            "b717e5b7-c177-42de-9d76-53c2c2ce817b\n"
            "groovy.lang.MissingPropertyException: No such property: source "
            "for class: groovy.lang.Binding\n"
            "at PluginClassLoader for workflow-cps//com.cloudbees.groovy.cps.impl."
            "PropertyAccessBlock.rawGet(PropertyAccessBlock.java:20)\n"
            "at WorkflowScript.run(WorkflowScript:114)\n"
        ),
        build={
            "job_name": "pingcap/tidb/merged_sqllogic_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "merged_sqllogic_test/136/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "JENKINS_GROOVY"
    assert classification.source == "rule:infra_jenkins_groovy"


def test_rule_engine_classifies_jenkins_dsl_error_as_groovy_infra() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "org.jenkinsci.plugins.workflow.cps.CpsCompilationErrorsException: "
            "startup failed:\n"
            "WorkflowScript: 48: No such DSL method 'podTemplatex' found among steps\n"
            "at WorkflowScript.run(WorkflowScript:48)\n"
        ),
        build={
            "job_name": "pingcap/tidb/merged_sqllogic_test",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/merged_sqllogic_test/137/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "JENKINS_GROOVY"
    assert classification.source == "rule:infra_jenkins_groovy"


def test_rule_engine_prefers_jenkins_cache_over_groovy_and_remoting_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "org.apache.http.ConnectionClosedException: Premature end of Content-Length "
            "delimited message body (expected: 6,160,424,960; received: 5,616,173,056)\n"
            "at hudson.remoting.RemoteInputStream.read(RemoteInputStream.java:296)\n"
            "Also: hudson.remoting.Channel$CallSiteStackTrace: Remote call to JNLP4-connect\n"
            "at PluginClassLoader for jenkins-pipeline-cache//"
            "io.jenkins.plugins.pipeline.cache.CacheStep$CacheStepExecution.start"
            "(CacheStep.java:117)\n"
            "at PluginClassLoader for workflow-cps//com.cloudbees.groovy.cps.impl."
            "ContinuationGroup.methodCall(ContinuationGroup.java:107)\n"
            "Caused: java.io.IOException: Failed to extract input stream\n"
            "at PluginClassLoader for jenkins-pipeline-cache//"
            "io.jenkins.plugins.pipeline.cache.agent.RestoreCallable.invoke"
            "(RestoreCallable.java:50)\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_mysql_client_test_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_mysql_client_test_next_gen/1813/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "JENKINS_CACHE"
    assert classification.source == "rule:infra_jenkins_cache"


def test_rule_engine_classifies_read_from_tar_extract_stream_failure_as_jenkins_cache() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "org.jenkinsci.plugins.workflow.actions.ErrorAction$ErrorId: 42\n"
            "at hudson.FilePath.readFromTar(FilePath.java:3111)\n"
            "Caused: java.io.IOException: Failed to extract input stream\n"
            "at hudson.FilePath.readFromTar(FilePath.java:3121)\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_mysql_test",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_mysql_test/2627/console",
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "JENKINS_CACHE"
    assert classification.source == "rule:infra_jenkins_cache"


def test_rule_engine_prefers_websocket_network_error_over_jenkins_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "java.io.EOFException\n"
            "at PluginClassLoader for okhttp-api//okio.RealBufferedSource.require"
            "(RealBufferedSource.kt:225)\n"
            "at PluginClassLoader for okhttp-api//okhttp3.internal.ws.WebSocketReader."
            "readHeader(WebSocketReader.kt:125)\n"
            "at PluginClassLoader for okhttp-api//okhttp3.internal.ws.RealWebSocket."
            "loopReader(RealWebSocket.kt:317)\n"
            "hudson.remoting.ChannelClosedException: channel is already closed\n"
            "ERROR: Process exited immediately after creation. Check logs above for more details.\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2095/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "NETWORK"
    assert classification.source == "rule:infra_network_websocket"


def test_rule_engine_classifies_jenkins_git_http_500_as_infra_git() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "ERROR: Error cloning remote repo 'origin'\n"
            "hudson.plugins.git.GitException: Command \"git fetch --tags --force --progress "
            "--depth=1 -- https://github.com/PingCAP-QE/ci.git "
            "+refs/heads/*:refs/remotes/origin/*\" returned status code 128:\n"
            "stdout:\n"
            "stderr: error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
            "fatal: expected flush after ref listing\n"
            "ERROR: Maximum checkout retry attempts reached, aborting\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/ticdc/pull_cdc_mysql_integration_light",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/ticdc/job/"
                "pull_cdc_mysql_integration_light/1867/console"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "GIT"
    assert classification.source == "rule:infra_git_checkout_failure"


def test_rule_engine_prefers_go_compile_error_over_jenkins_remoting_warning() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@574dcfaf:JNLP4-connect]\n"
            "CGO_ENABLED=1 GO111MODULE=on go build -tags codes,nextgen "
            "-o 'bin/tidb-server' ./cmd/tidb-server\n"
            "# github.com/pingcap/tidb/pkg/util/etcd\n"
            "pkg/util/etcd/source.go:31:67: undefined: rpctypes.MetadataClientSourceKey\n"
            "pkg/util/etcd/source.go:34:22: undefined: rpctypes.MetadataClientSourceKey\n"
            "make: *** [Makefile:249: server] Error 1\n"
            "ERROR: script returned exit code 2\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_mysql_client_test_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_mysql_client_test_next_gen/1789/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "COMPILE"
    assert classification.source == "rule:build_go_compile_failure"


def test_rule_engine_matches_go_missing_field_compile_error_shape() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "CGO_ENABLED=1 GO111MODULE=on go build -o 'bin/tidb-server' ./cmd/tidb-server\n"
            "# github.com/pingcap/tidb/pkg/sessionctx/stmtctx\n"
            "pkg/sessionctx/stmtctx/stmtctx.go:670:5: "
            "sc.AlternativeLogicalPlanPreferCorrelate undefined "
            "(type *StatementContext has no field or method AlternativeLogicalPlanPreferCorrelate)\n"
            "make: *** [Makefile:249: server] Error 1\n"
            "ERROR: script returned exit code 2\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_mysql_client_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_mysql_client_test/1768/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "COMPILE"
    assert classification.source == "rule:build_go_compile_failure"


def test_rule_engine_matches_go_mod_tidy_dependency_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "CGO_ENABLED=1 GO111MODULE=on go build -tags codes,nextgen "
            "-o 'bin/tidb-server' ./cmd/tidb-server\n"
            "go: updates to go.mod needed; to update it:\n"
            "\tgo mod tidy\n"
            "make: *** [Makefile:249: server] Error 1\n"
            "ERROR: script returned exit code 2\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_mysql_client_test_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_mysql_client_test_next_gen/1511/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "DEPENDENCY"
    assert classification.source == "rule:build_dependency_failure"


def test_rule_engine_matches_bazel_strict_dependency_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "ERROR: /home/jenkins/agent/workspace/pingcap/tidb/pull_build_next_gen/tidb/"
            "pkg/sessionctx/stmtctx/BUILD.bazel:38:8: "
            "GoCompilePkg pkg/sessionctx/stmtctx/importer.recompile1498.a failed\n"
            "compilepkg: missing strict dependencies:\n"
            "\t/home/jenkins/.../pkg/executor/importer/import.go: import of "
            "\"github.com/pingcap/tidb/pkg/util/timeutil\"\n"
            "No dependencies were provided.\n"
            "Check that imports in Go sources match importpath attributes in deps.\n"
            "make: *** [Makefile:744: bazel_build] Error 1\n"
            "ERROR: script returned exit code 2\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_build_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_build_next_gen/2012/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "DEPENDENCY"
    assert classification.source == "rule:build_dependency_failure"


def test_rule_engine_prefers_go_plugin_abi_mismatch_over_jenkins_remoting_warning() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@33e50a9c:JNLP4-connect]\n"
            "go run ./cmd/pluginpkg -next-gen -pkg-dir ../enterprise-plugin/audit "
            "-out-dir ../plugin-so\n"
            "Package \"../enterprise-plugin/audit\" as plugin \"../plugin-so/audit-1.so\" "
            "success.\n"
            "[FATAL] [terror.go:309] [\"unexpected error\"] "
            "[error=\"plugin.Open(\\\"plugin-so/audit-1\\\"): plugin was built with "
            "a different version of package "
            "github.com/apache/arrow-go/v18/arrow/internal/debug\"]\n"
            "ERROR: script returned exit code 1\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_build_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_build_next_gen/1932/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "COMPILE"
    assert classification.source == "rule:build_go_compile_failure"


def test_rule_engine_prefers_codegen_failure_over_k8s_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Pod [Pending][Unschedulable] 0/90 nodes are available: "
            "76 Insufficient memory.\n"
            "go build -C generate_keyword -o ../genkeyword\n"
            "go generate\n"
            "pkg/planner/cardinality/test/selectivity_test.go\n"
            "pkg/planner/core/casetest/rule/test/register_gen.go\n"
            "Your commit is changed after running go generate ./..., "
            "it should not happen.\n"
            "make: *** [Makefile:81: gogenerate] Error 1\n"
            "ERROR: script returned exit code 2\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check/1837/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "CODEGEN"
    assert classification.source == "rule:build_codegen_failure"


def test_rule_engine_prefers_lint_failure_over_k8s_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Pod [Pending][Unschedulable] 0/90 nodes are available: "
            "76 Insufficient memory.\n"
            "linting\n"
            "  https://revive.run/r#exported exported function InitForMega "
            "should have comment or be unexported\n"
            "  pkg/bindinfo/test/main.go:30:1\n"
            "6010 problems (6010 errors, 0 warnings)\n"
            "Errors:\n"
            "  5823  exported\n"
            "make: *** [Makefile:91: lint] Error 255\n"
            "ERROR: script returned exit code 2\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check/1193/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "LINT"
    assert classification.source == "rule:build_lint_failure"


def test_rule_engine_ignores_transient_failed_scheduling_without_final_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning  FailedScheduling  default-scheduler  "
            "0/90 nodes are available: 76 Insufficient memory, "
            "1 node(s) had untolerated taint {ToBeDeletedByClusterAutoscaler: 1777537003}; "
            "preemption: 0/90 nodes are available.\n"
            "Pod [Pending][Unschedulable] 0/90 nodes are available: "
            "1 node(s) had untolerated taint {ToBeDeletedByClusterAutoscaler: 1777537003}, "
            "76 Insufficient memory, pod has unbound immediate PersistentVolumeClaims. "
            "preemption: 0/90 nodes are available.\n"
            "Container [golang] waiting [ContainerCreating] No message\n"
            "Pod [Pending][ContainersNotReady] containers with unready status: [golang jnlp]\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2600/",
        },
    )

    assert classification is None


def test_rule_engine_does_not_treat_evicted_test_names_as_k8s_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "PASSED stmtsummary.TestStmtSummaryByDigestEvicted (0.0s)\n"
            "PASSED reporter.Test_collecting_markAsEvicted_hasEvicted (0.0s)\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2601/",
        },
    )

    assert classification is None


def test_rule_engine_prefers_admin_abort_over_downstream_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Warning: JENKINS-30600: special launcher decorates "
            "RemoteLauncher[hudson.remoting.Channel@574dcfaf:JNLP4-connect]\n"
            "pkg/util/etcd/source.go:31:67: undefined: rpctypes.MetadataClientSourceKey\n"
            "Aborted by Flare Zuo\n"
            "Sending interrupt signal to process\n"
            "script returned exit code 143\n"
            "Finished: ABORTED\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2153/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "ABORT_BY_ADMIN"
    assert classification.source == "rule:others_abort_by_admin"


def test_rule_engine_prefers_superseded_abort_metadata_over_log_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "WorkflowScript: 42: unexpected token: foo\n"
            "Aborted by Flare Zuo\n"
            "script returned exit code 143\n"
            "Finished: ABORTED\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2598/",
            "prow_state": "aborted",
            "prow_status_description": "Aborted as the newer version of this job is running.",
            "has_newer_pr_job_version": "1",
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "SUPERSEDED_BY_NEWER_BUILD"
    assert classification.source == "rule:others_superseded_by_prow_newer_build"


def test_rule_engine_classifies_trigger_plugin_abort_with_newer_sha() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text="",
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2641/",
            "prow_state": "aborted",
            "prow_status_description": "Aborted by trigger plugin.",
            "has_newer_pr_job_version_with_different_sha": "1",
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "SUPERSEDED_BY_NEWER_BUILD"
    assert classification.source == "rule:others_superseded_by_trigger_plugin_newer_sha"


def test_rule_engine_classifies_generic_admin_abort_with_newer_sha_evidence() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Aborted by Flare Zuo\n"
            "Sending interrupt signal to process\n"
            "Finished: ABORTED\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2630/",
            "prow_state": "aborted",
            "prow_status_description": "Jenkins job aborted.",
            "has_newer_pr_job_version_with_different_sha": "1",
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "SUPERSEDED_BY_NEWER_BUILD"
    assert classification.source == "rule:others_superseded_by_admin_abort_newer_sha"


def test_rule_engine_keeps_failed_prow_admin_abort_as_admin_even_with_newer_sha() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Aborted by Flare Zuo\n"
            "Sending interrupt signal to process\n"
            "Finished: ABORTED\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2669/",
            "prow_state": "failure",
            "prow_status_description": "Jenkins job failed.",
            "has_newer_pr_job_version_with_different_sha": "1",
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "ABORT_BY_ADMIN"
    assert classification.source == "rule:others_abort_by_admin"


def test_rule_engine_does_not_call_admin_abort_superseded_without_newer_build_evidence() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Aborted by Flare Zuo\n"
            "script returned exit code 143\n"
            "Finished: ABORTED\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2598/",
            "prow_state": "aborted",
            "prow_status_description": "Aborted as the newer version of this job is running.",
            "has_newer_pr_job_version": "0",
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "ABORT_BY_ADMIN"
    assert classification.source == "rule:others_abort_by_admin"


def test_rule_engine_prefers_br_admin_abort_over_matrix_and_network_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "url=https://127.0.0.1:2379 error=\"transport: Error while dialing: "
            "dial tcp 127.0.0.1:2379: connect: connection refused\"\n"
            "context deadline exceeded\n"
            "Aborted by \x1b[8mha:////4ErKGn83PyjhowML0pM2b0wH1es5oBrFC8fDU/WnulZY\x1b[0mFlare Zuo\n"
            "Sending interrupt signal to process\n"
            "script returned exit code 143\n"
            "Failed in branch Matrix - TEST_GROUP = 'G02'\n"
            "Failed in branch Matrix - TEST_GROUP = 'G03'\n"
            "Finished: ABORTED\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_br_integration_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_br_integration_test/830/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "OTHERS"
    assert classification.l2_subcategory == "ABORT_BY_ADMIN"
    assert classification.source == "rule:others_abort_by_admin"


def test_rule_engine_returns_none_on_rule_miss() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text="some brand new unknown failure shape",
        build={"job_name": "mystery_job", "url": "https://prow.tidb.net/job/x"},
    )

    assert classification is None


def test_rule_engine_classifies_pull_unit_rewrite_error_as_build_compile() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "go: downloading github.com/pingcap/failpoint v0.0.0-20240527053858-9b3b6e34194a\n"
            "Rewrite error /home/jenkins/agent/workspace/pingcap/tidb/release-8.5/"
            "pull_unit_test/tidb/pkg/planner/core/find_best_task.go:2814:1: "
            "expected statement, found '<<' (and 10 more errors)\n"
            "make: *** [Makefile:292: failpoint-enable] Error 123\n"
            "Finished: FAILURE\n"
        ),
        build={
            "job_name": "pingcap/tidb/release-8.5/pull_unit_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "release-8.5/job/pull_unit_test/2507/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "COMPILE"
    assert classification.source == "rule:build_go_compile_failure"


def test_rule_engine_prefers_build_compile_over_bazel_unit_wrapper_failure() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "pkg/util/etcd/source.go:31:67: undefined: rpctypes.MetadataClientSourceKey\n"
            "pkg/util/etcd/source.go:34:22: undefined: rpctypes.MetadataClientSourceKey\n"
            "compilepkg: error running subcommand external/go_sdk/pkg/tool/linux_amd64/compile: "
            "exit status 2\n"
            "//br/pkg/checkpoint:checkpoint_test FAILED TO BUILD\n"
            "Test cases: finished with 0 passing and 0 failing out of 0 test cases\n"
            "Executed 0 out of 530 tests: 1 fails to build and 529 were skipped.\n"
            "make: *** [Makefile:705: bazel_ci_test] Error 1\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_unit_test",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/3000/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "COMPILE"
    assert classification.source == "rule:build_go_compile_failure"


def test_rule_engine_classifies_pull_unit_nogo_validation_as_format_check() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "ERROR: /home/jenkins/agent/workspace/pingcap/tidb/pull_unit_test_next_gen/"
            "tidb/pkg/inference/BUILD.bazel:3:11: Validating nogo output for "
            "//pkg/inference:inference failed: (Exit 1): builder failed\n"
            "pkg/inference/openai.go:139:28: unnecessary conversion (unconvert)\n"
            "make: *** [Makefile:718: bazel_coverage_test] Error 1\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_unit_test_next_gen",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_unit_test_next_gen/1514/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "FORMAT_CHECK"
    assert classification.source == "rule:unit_format_check_nogo_failure"


def test_rule_engine_classifies_unit_failed_to_build_without_explicit_compile_line() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "//pkg/planner/core:core_test FAILED TO BUILD\n"
            "Test cases: finished with 5749 passing and 0 failing out of 5749 test cases\n"
            "Executed 3 out of 528 tests: 278 tests pass, 1 fails to build and 249 were skipped.\n"
            "make: *** [Makefile:726: bazel_coverage_test_ddlargsv1] Error 1\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_unit_test_ddlv1",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_unit_test_ddlv1/853/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "BUILD"
    assert classification.l2_subcategory == "COMPILE"
    assert classification.source == "rule:unit_bazel_failed_to_build"


def test_rule_engine_prefers_external_dependency_over_ghpr_check2_noise() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "Failed in branch Matrix - SCRIPT_AND_ARGS = 'run_real_tikv_tests.sh "
            "bazel_pessimistictest'\n"
            "script returned exit code 143\n"
            "WARNING: Download from https://github.com/bazel-contrib/bazel_features/"
            "releases/download/v1.15.0/bazel_features-v1.15.0.tar.gz failed: class "
            "com.google.devtools.build.lib.bazel.repository.downloader."
            "UnrecoverableHttpException GET returned 502 Bad Gateway or Proxy Error\n"
            "ERROR: An error occurred during the fetch of repository 'bazel_features':\n"
            "Error in download_and_extract: java.io.IOException: Error downloading "
            "[https://github.com/bazel-contrib/bazel_features/releases/download/v1.15.0/"
            "bazel_features-v1.15.0.tar.gz] to /tmp/bazel_features-v1.15.0.tar.gz: "
            "GET returned 502 Bad Gateway or Proxy Error\n"
            "ERROR: Error computing the main repository mapping: no such package "
            "'@bazel_features//': java.io.IOException: Error downloading "
            "[https://github.com/bazel-contrib/bazel_features/releases/download/v1.15.0/"
            "bazel_features-v1.15.0.tar.gz] to /tmp/bazel_features-v1.15.0.tar.gz: "
            "GET returned 502 Bad Gateway or Proxy Error\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2523/",
        },
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "EXTERNAL_DEP"
    assert classification.source == "rule:infra_external_dependency_bazel_fetch_failure"


def test_rule_engine_does_not_treat_passed_test_names_with_fail_suffix_as_failures() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "PASSED  sessionstates.TestShowStateFail (6.4s)\n"
            "PASSED  sessionstates.TestSQLBindingCompatibility (4.3s)\n"
            "Finished: SUCCESS\n"
        ),
        build={
            "job_name": "pingcap/tidb/ghpr_unit_test",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2398/",
        },
    )

    assert classification is None


def test_rule_engine_does_not_treat_lightning_disk_quota_grep_as_disk_full() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text=(
            "+ grep -q 'disk quota exceeded' /tmp/lightning_test/lightning-disk-quota.log\n"
            "TEST FAILED: LIGHTNING LOG DOES NOT CONTAIN "
            "'Experiencing a wait timeout while writing to tikv'\n"
            "Failed in branch Matrix - TEST_GROUP = 'G08'\n"
            "script returned exit code 1\n"
        ),
        build={
            "job_name": "pingcap/tidb/pull_lightning_integration_test",
            "url": (
                "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/"
                "pull_lightning_integration_test/1018/"
            ),
        },
    )

    assert classification is not None
    assert classification.l1_category == "IT"
    assert classification.l2_subcategory == "TEST_FAILURE"
    assert classification.source == "rule:lightning_integration_test_failure"
