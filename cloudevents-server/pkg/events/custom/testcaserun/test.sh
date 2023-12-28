#!/usr/bin/env bash

curl --verbose --request POST \
    --url http://127.0.0.1:8080/events \
    --header "ce-id: $(uuidgen)" \
    --header 'ce-source: https://do.pingcap.net/jenkins' \
    --header 'ce-type: test-case-run-report' \
    --header 'ce-repo: pingcap/tidb' \
    --header 'ce-branch: master' \
    --header 'ce-buildurl: https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/35602/' \
    --header 'ce-specversion: 1.0' \
    --header 'content-type: application/json; charset=UTF-8' \
    --data @bazel-go-test-problem-cases.json
