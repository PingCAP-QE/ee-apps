#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/roster-jobs:latest"
cronjob_name="roster-sync"
schedule="0 3 * * *"
time_zone="Asia/Shanghai"
db_secret="ci-dashboard-eq-prd-insight-db"
lark_secret="roster-lark"
log_level="INFO"
image_pull_policy="IfNotPresent"
service_account=""
ca_secret=""
ca_key="ca.crt"
cpu_request="250m"
memory_request="512Mi"
cpu_limit="1"
memory_limit="1Gi"
successful_jobs_history="3"
failed_jobs_history="3"
backoff_limit="1"
active_deadline_seconds="3600"
starting_deadline_seconds="1800"
concurrency_policy="Forbid"
suspend="false"

usage() {
  cat <<'EOF'
Render a Kubernetes CronJob manifest for roster sync.

Usage:
  render_roster_sync_cronjob.sh [options]

Optional:
  --namespace NAME             Kubernetes namespace. Default: apps
  --image IMAGE                Jobs image. Default: ghcr.io/pingcap-qe/ee-apps/roster-jobs:latest
  --cronjob-name NAME          CronJob name. Default: roster-sync
  --schedule CRON              Cron expression. Default: "0 3 * * *"
  --time-zone TZ               CronJob timeZone. Default: Asia/Shanghai
  --db-secret NAME             Secret containing ROSTER_DB_URL, ROSTER_TIDB_*, CI_DASHBOARD_DB_URL, or TIDB_* keys. Default: ci-dashboard-eq-prd-insight-db
  --lark-secret NAME           Secret containing ROSTER_LARK_* keys. Default: roster-lark
  --log-level LEVEL            ROSTER_LOG_LEVEL override. Default: INFO
  --image-pull-policy P        Image pull policy. Default: IfNotPresent
  --service-account NAME       Optional service account name.
  --ca-secret NAME             Optional secret containing CA file for TiDB SSL.
  --ca-key NAME                Key inside CA secret. Default: ca.crt
  --cpu-request VALUE          CPU request. Default: 250m
  --memory-request VALUE       Memory request. Default: 512Mi
  --cpu-limit VALUE            CPU limit. Default: 1
  --memory-limit VALUE         Memory limit. Default: 1Gi
  --successful-history N       successfulJobsHistoryLimit. Default: 3
  --failed-history N           failedJobsHistoryLimit. Default: 3
  --backoff-limit N            Job backoffLimit. Default: 1
  --active-deadline N          activeDeadlineSeconds. Default: 3600
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
    --lark-secret)
      lark_secret="${2:-}"
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
                - name: ROSTER_TIDB_SSL_CA
                  value: /var/run/roster/ssl/${ca_key}
EOF
)
  ca_mount_block=$(cat <<EOF
              volumeMounts:
                - name: tidb-ca
                  mountPath: /var/run/roster/ssl
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
    app.kubernetes.io/name: roster-sync
    app.kubernetes.io/part-of: roster
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
            app.kubernetes.io/name: roster-sync
            app.kubernetes.io/part-of: roster
        spec:
          restartPolicy: Never
${service_account_block}
          containers:
            - name: sync-roster
              image: ${image}
              imagePullPolicy: ${image_pull_policy}
              args:
                - sync-roster
              envFrom:
                - secretRef:
                    name: ${db_secret}
                - secretRef:
                    name: ${lark_secret}
              env:
                - name: PYTHONUNBUFFERED
                  value: "1"
                - name: ROSTER_LOG_LEVEL
                  value: "${log_level}"
${ca_env_block}
              resources:
                requests:
                  cpu: "${cpu_request}"
                  memory: "${memory_request}"
                limits:
                  cpu: "${cpu_limit}"
                  memory: "${memory_limit}"
${ca_mount_block}
${ca_volume_block}
EOF
