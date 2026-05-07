# Jenkins Error Classification Review Log

- Range: `2026-04-20` to `2026-05-06` (`start_time < 2026-05-07`)
- Sampling rule: top 3 jobs by err count within each `L1/L2`; sample 3-5 builds from latest 20 error builds; skip category/job when total builds < 5.
- Task groups: `44`
- Sample builds: `183`
- Task file: `/Users/dillon/workspace/ee-apps-worktrees/v7-pod-UI/ci-dashboard/.local/jenkins-error-review/tasks_2026-04-20_2026-05-06.json`

## Reviewed Groups

- [ ] BUILD/COMPILE | pingcap/tidb/pull_unit_test_next_gen | err_count=13 | sample_ids=1711574, 881607, 1649277, 878529
- [ ] BUILD/COMPILE | pingcap/tidb/ghpr_unit_test | err_count=9 | sample_ids=1711580, 1649270, 878193
- [ ] BUILD/COMPILE | pingcap/tidb/release-8.5/pull_unit_test | err_count=7 | sample_ids=1770816, 1710039, 1646273
- [ ] BUILD/DEPENDENCY | pingcap/tidb/ghpr_build | err_count=9 | sample_ids=1712934, 1712565, 1771016
- [ ] BUILD/DEPENDENCY | pingcap/tidb/pull_build_next_gen | err_count=9 | sample_ids=1712567, 1712474, 1770643
- [ ] BUILD/LINT | pingcap/tidb/ghpr_build | err_count=7 | sample_ids=1712931, 1712513, 1770507
- [ ] BUILD/LINT | pingcap/tidb/pull_build_next_gen | err_count=7 | sample_ids=1713113, 1712921, 1712514
- [ ] BUILD/PIPELINE_CONFIG | pingcap/tidb/ghpr_build | err_count=91 | sample_ids=1740130, 1740026, 1683205, 1710856, 1710838
- [ ] BUILD/PIPELINE_CONFIG | pingcap/tidb/pull_build_next_gen | err_count=88 | sample_ids=1711889, 1711518, 1740022, 1711136, 1682351
- [ ] BUILD/PIPELINE_CONFIG | pingcap/tidb/ghpr_check | err_count=77 | sample_ids=1711639, 1711577, 1683454, 1710866, 1710817
- [ ] INFRA/DISK_FULL | pingcap/tidb/pull_unit_test_next_gen | err_count=31 | sample_ids=273680, 1650724, 273609, 273537, 273505
- [ ] INFRA/DISK_FULL | pingcap/tidb/ghpr_unit_test | err_count=27 | sample_ids=273714, 273738, 1650701, 1650514, 273482
- [ ] INFRA/DISK_FULL | pingcap/tidb/ghpr_check2 | err_count=26 | sample_ids=1770701, 273664, 273590, 273620, 1650633
- [ ] INFRA/EXTERNAL_DEP | pingcap/tidb/ghpr_check2 | err_count=22 | sample_ids=1770118, 1711220, 1711051, 1710858, 1710443
- [ ] INFRA/EXTERNAL_DEP | pingcap/tidb/ghpr_check | err_count=10 | sample_ids=1710829, 1710569, 1682452, 1681164
- [ ] INFRA/JENKINS | pingcap/ticdc/pull_cdc_mysql_integration_light | err_count=25 | sample_ids=1713116, 1713014, 1712992, 1712963, 1712856
- [ ] INFRA/JENKINS | pingcap/tidb/pull_mysql_client_test_next_gen | err_count=8 | sample_ids=1681222, 881559, 878028
- [ ] INFRA/JENKINS_CACHE | pingcap/tidb/ghpr_mysql_test | err_count=24 | sample_ids=1770870, 1712123, 1710458, 1710310, 1710093
- [ ] INFRA/JENKINS_CACHE | pingcap/tidb/release-8.5/pull_check2 | err_count=8 | sample_ids=1950014, 880710, 878955
- [ ] INFRA/JENKINS_CACHE | pingcap/tidb/pull_mysql_client_test_next_gen | err_count=7 | sample_ids=1770602, 1683494, 1681836
- [ ] INFRA/JENKINS_GROOVY | pingcap/tidb/merged_sqllogic_test | err_count=64 | sample_ids=1712844, 1770897, 1770466, 1711324, 1710978
- [ ] INFRA/JENKINS_GROOVY | pingcap/tidb/merged_integration_br_test | err_count=27 | sample_ids=1712618, 1770872, 1770756, 1770460, 1711821
- [ ] INFRA/JENKINS_GROOVY | pingcap/tidb/merged_integration_lightning_test | err_count=27 | sample_ids=1950114, 1770957, 1770873, 1711822, 1683281
- [ ] INFRA/K8S_MEMORY_EVICTION | pingcap/tidb/ghpr_check2 | err_count=54 | sample_ids=1712646, 1712378, 1711529, 1740307, 1740217
- [ ] INFRA/NETWORK | pingcap/tidb/ghpr_check2 | err_count=12 | sample_ids=1682057, 1710348, 881160, 878520
- [ ] INFRA/NETWORK | pingcap/tidb/merged_integration_br_test | err_count=10 | sample_ids=878176, 1647681, 1646680, 877853
- [ ] INFRA/NETWORK | pingcap/tidb/pull_lightning_integration_test | err_count=6 | sample_ids=1920022, 1712604, 1712231
- [ ] INFRA/OOMKILLED | pingcap/ticdc/pull_cdc_mysql_integration_light | err_count=20 | sample_ids=1950171, 1713162, 1713097, 1713008, 1650513
- [ ] INFRA/OOMKILLED | pingcap/tiflow/pull_cdc_integration_storage_test | err_count=8 | sample_ids=1712758, 1655366, 882200
- [ ] IT/TEST_FAILURE | pingcap/tidb/pull_integration_realcluster_test_next_gen | err_count=137 | sample_ids=1713105, 1712626, 1712521, 1712397, 1740280
- [ ] IT/TEST_FAILURE | tidbcloud/cloud-storage-engine/dedicated/pull_integration_realcluster_test_next_gen | err_count=66 | sample_ids=1712770, 1712536, 1712260, 1712142, 1712031
- [ ] IT/TEST_FAILURE | pingcap/tidb/ghpr_check2 | err_count=59 | sample_ids=1711997, 1711882, 1740194, 1711329, 1683559
- [ ] IT/TIMEOUT | pingcap/tidb/pull_br_integration_test | err_count=6 | sample_ids=1712269, 881950, 880519
- [ ] IT/TIMEOUT | pingcap/tiflow/pull_cdc_integration_pulsar_test | err_count=6 | sample_ids=1770078, 882199, 880608
- [ ] OTHERS/ABORT_BY_ADMIN | tidbcloud/cloud-storage-engine/dedicated/pull_integration_realcluster_test_next_gen | err_count=9 | sample_ids=1950084, 1712861, 1890002
- [ ] OTHERS/SUPERSEDED_BY_NEWER_BUILD | pingcap/tidb/pull_integration_realcluster_test_next_gen | err_count=164 | sample_ids=1712430, 1712416, 1770342, 1712080, 1712044
- [ ] OTHERS/SUPERSEDED_BY_NEWER_BUILD | pingcap/tidb/ghpr_check2 | err_count=146 | sample_ids=1890021, 1770883, 1770852, 1712415, 1770337
- [ ] OTHERS/SUPERSEDED_BY_NEWER_BUILD | pingcap/tidb/pull_unit_test_next_gen | err_count=143 | sample_ids=1890017, 1712394, 1712358, 1712094, 1712060
- [ ] OTHERS/UNCLASSIFIED | pingcap/tidb/ghpr_unit_test | err_count=15 | sample_ids=1682497, 1680417, 880874, 879885
- [ ] OTHERS/UNCLASSIFIED | pingcap/tidb/pull_unit_test_next_gen | err_count=9 | sample_ids=1680423, 877467, 272494
- [ ] OTHERS/UNCLASSIFIED | pingcap/tidb/pull_unit_test_ddlv1 | err_count=7 | sample_ids=1682346, 881357, 880970
- [ ] UT/TEST_FAILURE | pingcap/tidb/ghpr_unit_test | err_count=424 | sample_ids=1713096, 1770593, 1711699, 1711546, 1711536
- [ ] UT/TEST_FAILURE | pingcap/tidb/pull_unit_test_ddlv1 | err_count=297 | sample_ids=1712976, 1770862, 1712268, 1770452, 1712243
- [ ] UT/TEST_FAILURE | pingcap/tidb/pull_unit_test_next_gen | err_count=290 | sample_ids=1713120, 1890024, 1712512, 1770603, 1770590

## Modified Cases

<!-- Append one line per changed case: build url| previous L1/L2| new L1/L2 -->

https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1311/|INFRA/NETWORK|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2299/|OTHERS/UNCLASSIFIED|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_ddlv1/750/|OTHERS/UNCLASSIFIED|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/2268/|BUILD/COMPILE|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2651/|OTHERS/UNCLASSIFIED|OTHERS/SUPERSEDED_BY_NEWER_BUILD
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/2394/|OTHERS/UNCLASSIFIED|OTHERS/SUPERSEDED_BY_NEWER_BUILD
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test_next_gen/1573/|INFRA/JENKINS|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2398/|INFRA/NETWORK|INFRA/EXTERNAL_DEP
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_ddlv1/844/|OTHERS/UNCLASSIFIED|OTHERS/ABORT_BY_ADMIN
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/1859/|BUILD/PIPELINE_CONFIG|OTHERS/ABORT_BY_ADMIN
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2823/|OTHERS/UNCLASSIFIED|OTHERS/SUPERSEDED_BY_NEWER_BUILD
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2199/|BUILD/PIPELINE_CONFIG|BUILD/DEPENDENCY
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2504/|IT/TEST_FAILURE|INFRA/EXTERNAL_DEP
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2383/|INFRA/NETWORK|INFRA/EXTERNAL_DEP
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check/2074/|BUILD/PIPELINE_CONFIG|BUILD/LINT
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2159/|BUILD/PIPELINE_CONFIG|BUILD/DEPENDENCY
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2167/|BUILD/PIPELINE_CONFIG|OTHERS/CODE_CONFLICT
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check/2085/|BUILD/PIPELINE_CONFIG|OTHERS/CODE_CONFLICT
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/1917/|BUILD/PIPELINE_CONFIG|BUILD/DEPENDENCY
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2523/|IT/TEST_FAILURE|INFRA/EXTERNAL_DEP
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/1968/|BUILD/PIPELINE_CONFIG|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2935/|UT/TEST_FAILURE|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2936/|UT/TEST_FAILURE|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check/2172/|BUILD/PIPELINE_CONFIG|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2571/|IT/TEST_FAILURE|UT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/2001/|BUILD/PIPELINE_CONFIG|BUILD/COMPILE
https://prow.tidb.net/jenkins/job/tidbcloud/job/cloud-storage-engine/job/dedicated/job/pull_integration_realcluster_test_next_gen/968/|IT/TEST_FAILURE|INFRA/EXTERNAL_DEP
https://prow.tidb.net/jenkins/job/tidbcloud/job/cloud-storage-engine/job/dedicated/job/pull_integration_realcluster_test_next_gen/973/|IT/TEST_FAILURE|OTHERS/ABORT_BY_ADMIN
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_lightning_integration_test/1004/|INFRA/NETWORK|IT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2189/|IT/TEST_FAILURE|OTHERS/SUPERSEDED_BY_NEWER_BUILD
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/2738/|UT/TEST_FAILURE|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/2061/|BUILD/LINT|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_lightning_integration_test/1018/|INFRA/NETWORK|IT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_integration_realcluster_test_next_gen/2229/|IT/TEST_FAILURE|OTHERS/ABORT_BY_ADMIN
https://prow.tidb.net/jenkins/job/tidbcloud/job/cloud-storage-engine/job/dedicated/job/pull_integration_realcluster_test_next_gen/1045/|OTHERS/ABORT_BY_ADMIN|OTHERS/SUPERSEDED_BY_NEWER_BUILD
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/2104/|BUILD/LINT|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/2113/|BUILD/LINT|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_build_next_gen/1946/|BUILD/PIPELINE_CONFIG|BUILD/DEPENDENCY
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2228/|BUILD/PIPELINE_CONFIG|BUILD/DEPENDENCY
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2231/|BUILD/PIPELINE_CONFIG|BUILD/DEPENDENCY
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2532/|IT/TEST_FAILURE|INFRA/EXTERNAL_DEP
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_lightning_integration_test/1021/|INFRA/NETWORK|IT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/tidbcloud/job/cloud-storage-engine/job/dedicated/job/pull_integration_realcluster_test_next_gen/1051/|OTHERS/ABORT_BY_ADMIN|OTHERS/SUPERSEDED_BY_NEWER_BUILD
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/2398/|OTHERS/UNCLASSIFIED|UT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_ddlv1/723/|OTHERS/UNCLASSIFIED|UT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check/2174/|BUILD/PIPELINE_CONFIG|UT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check/2120/|BUILD/PIPELINE_CONFIG|UT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2635/|INFRA/DISK_FULL|UT/TEST_FAILURE
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2310/|BUILD/LINT|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2334/|BUILD/LINT|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_build/2376/|BUILD/LINT|UT/FORMAT_CHECK
https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2581/|IT/TEST_FAILURE|UT/TEST_FAILURE

## Remaining Ambiguous Cases

- Remaining mismatches after DB rerun on 174 accessible sampled builds: `0`
- IDs to continue from: none
