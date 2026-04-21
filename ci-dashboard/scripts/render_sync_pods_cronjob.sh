#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest"
cronjob_name="ci-dashboard-sync-pods"
schedule="15 * * * *"
time_zone="Asia/Shanghai"
db_secret="ci-dashboard-db"
gcp_project=""
batch_size="200"
pod_event_namespaces="prow-test-pods"
overlap_minutes="15"
lookback_minutes="120"
max_pages="200"
log_level="INFO"
image_pull_policy="IfNotPresent"
service_account=""
ca_secret=""
ca_key="ca.crt"
cpu_request="500m"
memory_request="1Gi"
cpu_limit="2"
memory_limit="4Gi"
successful_jobs_history="3"
failed_jobs_history="3"
backoff_limit="1"
active_deadline_seconds="2700"
starting_deadline_seconds="1800"
concurrency_policy="Forbid"
suspend="false"

usage() {
  cat <<'EOF'
Render a Kubernetes CronJob manifest for recurring pod lifecycle sync.

Usage:
  render_sync_pods_cronjob.sh [options]

Optional:
  --namespace NAME             Kubernetes namespace. Default: apps
  --image IMAGE                Jobs image. Default: ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest
  --cronjob-name NAME          CronJob name. Default: ci-dashboard-sync-pods
  --schedule CRON              Cron expression. Default: "15 * * * *"
  --time-zone TZ               CronJob timeZone. Default: Asia/Shanghai
  --db-secret NAME             Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-db
  --gcp-project ID             GCP project used for Cloud Logging reads. Required.
  --batch-size N               CI_DASHBOARD_BATCH_SIZE override. Default: 200
  --pod-event-namespaces CSV   Comma-separated pod event namespaces. Default: prow-test-pods
  --overlap-minutes N          Overlap reread window. Default: 15
  --lookback-minutes N         Default lookback when no watermark exists. Default: 120
  --max-pages N                Max Logging API pages per run. Default: 200
  --log-level LEVEL            CI_DASHBOARD_LOG_LEVEL override. Default: INFO
  --image-pull-policy P        Image pull policy. Default: IfNotPresent
  --service-account NAME       Optional service account name.
  --ca-secret NAME             Optional secret containing CA file for TIDB SSL.
  --ca-key NAME                Key inside CA secret. Default: ca.crt
  --cpu-request VALUE          CPU request. Default: 500m
  --memory-request VALUE       Memory request. Default: 1Gi
  --cpu-limit VALUE            CPU limit. Default: 2
  --memory-limit VALUE         Memory limit. Default: 4Gi
  --successful-history N       successfulJobsHistoryLimit. Default: 3
  --failed-history N           failedJobsHistoryLimit. Default: 3
  --backoff-limit N            Job backoffLimit. Default: 1
  --active-deadline N          activeDeadlineSeconds. Default: 2700
  --starting-deadline N        startingDeadlineSeconds. Default: 1800
  --concurrency-policy VALUE   CronJob concurrencyPolicy. Default: Forbid
  --suspend true|false         Suspend CronJob. Default: false
  --help                       Show this help text.
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
    --gcp-project)
      gcp_project="${2:-}"
      shift 2
      ;;
    --batch-size)
      batch_size="${2:-}"
      shift 2
      ;;
    --pod-event-namespaces)
      pod_event_namespaces="${2:-}"
      shift 2
      ;;
    --overlap-minutes)
      overlap_minutes="${2:-}"
      shift 2
      ;;
    --lookback-minutes)
      lookback_minutes="${2:-}"
      shift 2
      ;;
    --max-pages)
      max_pages="${2:-}"
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

if [[ -z "${gcp_project}" ]]; then
  echo "--gcp-project is required" >&2
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

cat <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ${cronjob_name}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/name: ci-dashboard-sync-pods
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
            app.kubernetes.io/name: ci-dashboard-sync-pods
            app.kubernetes.io/part-of: ci-dashboard
        spec:
          restartPolicy: Never
${service_account_block}
          containers:
            - name: sync-pods
              image: ${image}
              imagePullPolicy: ${image_pull_policy}
              args:
                - sync-pods
              envFrom:
                - secretRef:
                    name: ${db_secret}
              env:
                - name: CI_DASHBOARD_GCP_PROJECT
                  value: ${gcp_project}
                - name: CI_DASHBOARD_BATCH_SIZE
                  value: "${batch_size}"
                - name: CI_DASHBOARD_POD_EVENT_NAMESPACES
                  value: "${pod_event_namespaces}"
                - name: CI_DASHBOARD_POD_SYNC_OVERLAP_MINUTES
                  value: "${overlap_minutes}"
                - name: CI_DASHBOARD_POD_SYNC_LOOKBACK_MINUTES
                  value: "${lookback_minutes}"
                - name: CI_DASHBOARD_POD_SYNC_MAX_PAGES
                  value: "${max_pages}"
                - name: CI_DASHBOARD_LOG_LEVEL
                  value: ${log_level}
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
