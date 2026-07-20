#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest"
db_secret="ci-dashboard-eq-prd-insight-db"
github_secret=""
github_token_key="token"
start_date=""
end_date=""
job_command="backfill-range"
batch_size="2000"
refresh_group_batch_size=""
log_level="INFO"
force_flaky_stale_cleanup="false"
job_name="ci-dashboard-backfill-$(date -u +%Y%m%d%H%M%S)"
image_pull_policy="IfNotPresent"
service_account=""
ca_secret=""
ca_key="ca.crt"
cpu_request="1"
memory_request="2Gi"
cpu_limit="4"
memory_limit="8Gi"
ttl_seconds="604800"
backoff_limit="1"
active_deadline_seconds="43200"

usage() {
  cat <<'EOF'
Render a one-off Kubernetes Job manifest for CI dashboard backfill.

Usage:
  render_backfill_job.sh --start-date YYYY-MM-DD [options]
  render_backfill_job.sh --job-command sync-flaky-issues [options]

Required:
  --start-date DATE         Inclusive start date for date-window job commands.

Optional:
  --job-command NAME        CLI subcommand. Default: backfill-range
  --end-date DATE           Inclusive end date for backfill-range.
  --namespace NAME          Kubernetes namespace. Default: apps
  --image IMAGE             Jobs image. Default: ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest
  --db-secret NAME          Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-eq-prd-insight-db
  --github-secret NAME      Optional secret containing GitHub API token.
  --github-token-key NAME   Key inside GitHub secret. Default: token
  --job-name NAME           Fixed Job name. Default: ci-dashboard-backfill-<timestamp>
  --batch-size N            CI_DASHBOARD_BATCH_SIZE override. Default: 2000
  --refresh-group-batch-size N
                            CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE override.
  --log-level LEVEL         CI_DASHBOARD_LOG_LEVEL override. Default: INFO
  --force-flaky-stale-cleanup true|false
                            Set CI_DASHBOARD_FORCE_FLAKY_STALE_CLEANUP. Default: false
  --image-pull-policy P     Image pull policy. Default: IfNotPresent
  --service-account NAME    Optional service account name.
  --ca-secret NAME          Optional secret containing CA file for TIDB SSL.
  --ca-key NAME             Key inside CA secret. Default: ca.crt
  --cpu-request VALUE       CPU request. Default: 1
  --memory-request VALUE    Memory request. Default: 2Gi
  --cpu-limit VALUE         CPU limit. Default: 4
  --memory-limit VALUE      Memory limit. Default: 8Gi
  --ttl-seconds N           ttlSecondsAfterFinished. Default: 604800
  --backoff-limit N         Job backoffLimit. Default: 1
  --active-deadline N       activeDeadlineSeconds. Default: 43200
  --help                    Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start-date)
      start_date="${2:-}"
      shift 2
      ;;
    --end-date)
      end_date="${2:-}"
      shift 2
      ;;
    --job-command)
      job_command="${2:-}"
      shift 2
      ;;
    --namespace)
      namespace="${2:-}"
      shift 2
      ;;
    --image)
      image="${2:-}"
      shift 2
      ;;
    --db-secret)
      db_secret="${2:-}"
      shift 2
      ;;
    --github-secret)
      github_secret="${2:-}"
      shift 2
      ;;
    --github-token-key)
      github_token_key="${2:-}"
      shift 2
      ;;
    --job-name)
      job_name="${2:-}"
      shift 2
      ;;
    --batch-size)
      batch_size="${2:-}"
      shift 2
      ;;
    --refresh-group-batch-size)
      refresh_group_batch_size="${2:-}"
      shift 2
      ;;
    --log-level)
      log_level="${2:-}"
      shift 2
      ;;
    --force-flaky-stale-cleanup)
      force_flaky_stale_cleanup="${2:-}"
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
    --ttl-seconds)
      ttl_seconds="${2:-}"
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

case "${force_flaky_stale_cleanup}" in
  true|false)
    ;;
  *)
    echo "--force-flaky-stale-cleanup must be true or false" >&2
    usage >&2
    exit 1
    ;;
esac

if [[ "${job_command}" == "sync-flaky-issues" || "${job_command}" == "backfill-flaky-issue-pr-links" ]]; then
  if [[ -n "${start_date}" || -n "${end_date}" ]]; then
    echo "${job_command} does not accept --start-date or --end-date" >&2
    usage >&2
    exit 1
  fi
elif [[ -z "${start_date}" ]]; then
  echo "--start-date is required for ${job_command}" >&2
  usage >&2
  exit 1
fi

args_block=$(cat <<EOF
            - ${job_command}
EOF
)
if [[ -n "${start_date}" ]]; then
  args_block+=$'\n'
  args_block+=$(cat <<EOF
            - --start-date
            - "${start_date}"
EOF
)
fi
if [[ -n "${end_date}" ]]; then
  args_block+=$'\n'
  args_block+=$(cat <<EOF
            - --end-date
            - "${end_date}"
EOF
)
fi

service_account_block=""
if [[ -n "${service_account}" ]]; then
  service_account_block=$(cat <<EOF
      serviceAccountName: ${service_account}
EOF
)
fi

refresh_group_env_block=""
if [[ -n "${refresh_group_batch_size}" ]]; then
  refresh_group_env_block=$(cat <<EOF
            - name: CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE
              value: "${refresh_group_batch_size}"
EOF
)
fi

github_env_block=""
if [[ -n "${github_secret}" ]]; then
  github_env_block=$(cat <<EOF
            - name: GITHUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${github_secret}
                  key: ${github_token_key}
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
kind: Job
metadata:
  name: ${job_name}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/name: ci-dashboard-backfill
    app.kubernetes.io/part-of: ci-dashboard
spec:
  backoffLimit: ${backoff_limit}
  activeDeadlineSeconds: ${active_deadline_seconds}
  ttlSecondsAfterFinished: ${ttl_seconds}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ci-dashboard-backfill
        app.kubernetes.io/part-of: ci-dashboard
    spec:
      restartPolicy: Never
${service_account_block}
      containers:
        - name: backfill
          image: ${image}
          imagePullPolicy: ${image_pull_policy}
          args:
${args_block}
          envFrom:
            - secretRef:
                name: ${db_secret}
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
${github_env_block}
            - name: CI_DASHBOARD_BATCH_SIZE
              value: "${batch_size}"
${refresh_group_env_block}
            - name: CI_DASHBOARD_FORCE_FLAKY_STALE_CLEANUP
              value: "${force_flaky_stale_cleanup}"
            - name: CI_DASHBOARD_LOG_LEVEL
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
