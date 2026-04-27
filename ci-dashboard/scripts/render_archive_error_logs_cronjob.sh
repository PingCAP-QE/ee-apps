#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest"
cronjob_name="ci-dashboard-archive-error-logs"
schedule="35 * * * *"
time_zone="Asia/Shanghai"
db_secret="ci-dashboard-db"
gcs_bucket=""
gcs_prefix="ci-dashboard/v3/jenkins-logs"
jenkins_internal_base_url=""
build_limit="100"
log_tail_bytes="262144"
log_level="INFO"
image_pull_policy="IfNotPresent"
service_account=""
ca_secret=""
ca_key="ca.crt"
jenkins_secret=""
jenkins_username_key="username"
jenkins_api_token_key="api-token"
cpu_request="500m"
memory_request="1Gi"
cpu_limit="1"
memory_limit="2Gi"
successful_jobs_history="3"
failed_jobs_history="3"
backoff_limit="1"
active_deadline_seconds="1800"
starting_deadline_seconds="1800"
concurrency_policy="Forbid"
suspend="false"

usage() {
  cat <<'EOF'
Render a Kubernetes CronJob manifest for recurring Jenkins error-log archival.

Usage:
  render_archive_error_logs_cronjob.sh [options]

Optional:
  --namespace NAME                Kubernetes namespace. Default: apps
  --image IMAGE                   Jobs image. Default: ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest
  --cronjob-name NAME             CronJob name. Default: ci-dashboard-archive-error-logs
  --schedule CRON                 Cron expression. Default: "35 * * * *"
  --time-zone TZ                  CronJob timeZone. Default: Asia/Shanghai
  --db-secret NAME                Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-db
  --gcs-bucket NAME               GCS bucket for archived logs. Required.
  --gcs-prefix PATH               GCS object prefix. Default: ci-dashboard/v3/jenkins-logs
  --jenkins-internal-base-url URL Internal Jenkins base URL. Required.
  --build-limit N                 Max builds archived per run. Default: 100
  --log-tail-bytes N              Tail byte cap. Default: 262144
  --log-level LEVEL               CI_DASHBOARD_LOG_LEVEL override. Default: INFO
  --image-pull-policy P           Image pull policy. Default: IfNotPresent
  --service-account NAME          Optional service account name.
  --ca-secret NAME                Optional secret containing CA file for TIDB SSL.
  --ca-key NAME                   Key inside CA secret. Default: ca.crt
  --jenkins-secret NAME           Optional secret containing Jenkins username/token.
  --jenkins-username-key NAME     Username key inside Jenkins secret. Default: username
  --jenkins-api-token-key NAME    API token key inside Jenkins secret. Default: api-token
  --cpu-request VALUE             CPU request. Default: 500m
  --memory-request VALUE          Memory request. Default: 1Gi
  --cpu-limit VALUE               CPU limit. Default: 1
  --memory-limit VALUE            Memory limit. Default: 2Gi
  --successful-history N          successfulJobsHistoryLimit. Default: 3
  --failed-history N              failedJobsHistoryLimit. Default: 3
  --backoff-limit N               Job backoffLimit. Default: 1
  --active-deadline N             activeDeadlineSeconds. Default: 1800
  --starting-deadline N           startingDeadlineSeconds. Default: 1800
  --concurrency-policy VALUE      CronJob concurrencyPolicy. Default: Forbid
  --suspend true|false            Suspend CronJob. Default: false
  --help                          Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      namespace="${2:-}"
      shift 2
      ;;
    --image)
      image="${2:-}"
      shift 2
      ;;
    --cronjob-name)
      cronjob_name="${2:-}"
      shift 2
      ;;
    --schedule)
      schedule="${2:-}"
      shift 2
      ;;
    --time-zone)
      time_zone="${2:-}"
      shift 2
      ;;
    --db-secret)
      db_secret="${2:-}"
      shift 2
      ;;
    --gcs-bucket)
      gcs_bucket="${2:-}"
      shift 2
      ;;
    --gcs-prefix)
      gcs_prefix="${2:-}"
      shift 2
      ;;
    --jenkins-internal-base-url)
      jenkins_internal_base_url="${2:-}"
      shift 2
      ;;
    --build-limit)
      build_limit="${2:-}"
      shift 2
      ;;
    --log-tail-bytes)
      log_tail_bytes="${2:-}"
      shift 2
      ;;
    --log-level)
      log_level="${2:-}"
      shift 2
      ;;
    --image-pull-policy)
      image_pull_policy="${2:-}"
      shift 2
      ;;
    --service-account)
      service_account="${2:-}"
      shift 2
      ;;
    --ca-secret)
      ca_secret="${2:-}"
      shift 2
      ;;
    --ca-key)
      ca_key="${2:-}"
      shift 2
      ;;
    --jenkins-secret)
      jenkins_secret="${2:-}"
      shift 2
      ;;
    --jenkins-username-key)
      jenkins_username_key="${2:-}"
      shift 2
      ;;
    --jenkins-api-token-key)
      jenkins_api_token_key="${2:-}"
      shift 2
      ;;
    --cpu-request)
      cpu_request="${2:-}"
      shift 2
      ;;
    --memory-request)
      memory_request="${2:-}"
      shift 2
      ;;
    --cpu-limit)
      cpu_limit="${2:-}"
      shift 2
      ;;
    --memory-limit)
      memory_limit="${2:-}"
      shift 2
      ;;
    --successful-history)
      successful_jobs_history="${2:-}"
      shift 2
      ;;
    --failed-history)
      failed_jobs_history="${2:-}"
      shift 2
      ;;
    --backoff-limit)
      backoff_limit="${2:-}"
      shift 2
      ;;
    --active-deadline)
      active_deadline_seconds="${2:-}"
      shift 2
      ;;
    --starting-deadline)
      starting_deadline_seconds="${2:-}"
      shift 2
      ;;
    --concurrency-policy)
      concurrency_policy="${2:-}"
      shift 2
      ;;
    --suspend)
      suspend="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${gcs_bucket}" ]]; then
  echo "--gcs-bucket is required" >&2
  exit 1
fi

if [[ -z "${jenkins_internal_base_url}" ]]; then
  echo "--jenkins-internal-base-url is required" >&2
  exit 1
fi

service_account_block=""
if [[ -n "${service_account}" ]]; then
  service_account_block=$(cat <<EOF
          serviceAccountName: ${service_account}
EOF
)
fi

time_zone_block=""
if [[ -n "${time_zone}" ]]; then
  time_zone_block=$(cat <<EOF
  timeZone: ${time_zone}
EOF
)
fi

ca_env_block=""
ca_mount_block=""
ca_volume_block=""
if [[ -n "${ca_secret}" ]]; then
  ca_env_block=$(cat <<EOF
                - name: TIDB_SSL_CA
                  value: /var/run/ci-dashboard/ssl/${ca_key}
EOF
)
  ca_mount_block=$(cat <<EOF
              volumeMounts:
                - name: tidb-ca
                  mountPath: /var/run/ci-dashboard/ssl
                  readOnly: true
EOF
)
  ca_volume_block=$(cat <<EOF
          volumes:
            - name: tidb-ca
              secret:
                secretName: ${ca_secret}
                items:
                  - key: ${ca_key}
                    path: ${ca_key}
EOF
)
fi

jenkins_auth_env_block=""
if [[ -n "${jenkins_secret}" ]]; then
  jenkins_auth_env_block=$(cat <<EOF
                - name: CI_DASHBOARD_JENKINS_USERNAME
                  valueFrom:
                    secretKeyRef:
                      name: ${jenkins_secret}
                      key: ${jenkins_username_key}
                - name: CI_DASHBOARD_JENKINS_API_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: ${jenkins_secret}
                      key: ${jenkins_api_token_key}
EOF
)
fi

cat <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ${cronjob_name}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/name: ci-dashboard-archive-error-logs
    app.kubernetes.io/part-of: ci-dashboard
spec:
  schedule: "${schedule}"
${time_zone_block}
  suspend: ${suspend}
  concurrencyPolicy: ${concurrency_policy}
  startingDeadlineSeconds: ${starting_deadline_seconds}
  successfulJobsHistoryLimit: ${successful_jobs_history}
  failedJobsHistoryLimit: ${failed_jobs_history}
  jobTemplate:
    spec:
      backoffLimit: ${backoff_limit}
      activeDeadlineSeconds: ${active_deadline_seconds}
      template:
        metadata:
          labels:
            app.kubernetes.io/name: ci-dashboard-archive-error-logs
            app.kubernetes.io/part-of: ci-dashboard
        spec:
${service_account_block}
          restartPolicy: Never
          containers:
            - name: archive-error-logs
              image: ${image}
              imagePullPolicy: ${image_pull_policy}
              args:
                - archive-error-logs
                - --limit
                - "${build_limit}"
              envFrom:
                - secretRef:
                    name: ${db_secret}
              env:
                - name: PYTHONUNBUFFERED
                  value: "1"
                - name: CI_DASHBOARD_LOG_LEVEL
                  value: ${log_level}
                - name: CI_DASHBOARD_GCS_BUCKET
                  value: ${gcs_bucket}
                - name: CI_DASHBOARD_GCS_PREFIX
                  value: ${gcs_prefix}
                - name: CI_DASHBOARD_ARCHIVE_LOG_TAIL_BYTES
                  value: "${log_tail_bytes}"
                - name: CI_DASHBOARD_JENKINS_INTERNAL_BASE_URL
                  value: ${jenkins_internal_base_url}
${jenkins_auth_env_block}
${ca_env_block}
              resources:
                requests:
                  cpu: "${cpu_request}"
                  memory: ${memory_request}
                limits:
                  cpu: "${cpu_limit}"
                  memory: ${memory_limit}
${ca_mount_block}
${ca_volume_block}
EOF
