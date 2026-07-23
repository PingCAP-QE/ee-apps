#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest"
cronjob_name="ci-dashboard-sync-unattached-block-volumes"
schedule="20 3 * * *"
time_zone="Asia/Shanghai"
db_secret="ci-dashboard-db"
aws_ebs_regions=""
aws_ebs_account_id=""
aws_owner_tag_keys=""
aws_secret=""
aws_access_key_id_key="aws-access-key-id"
aws_secret_access_key_key="aws-secret-access-key"
aws_session_token_key=""
gcp_projects=""
gcp_owner_label_keys=""
gcp_access_token_secret=""
gcp_access_token_key="access-token"
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
Render a Kubernetes CronJob manifest for recurring unattached block volume sync.

Usage:
  render_unattached_block_volumes_cronjob.sh [options]

Optional:
  --namespace NAME                  Kubernetes namespace. Default: apps
  --image IMAGE                     Jobs image. Default: ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest
  --cronjob-name NAME               CronJob name. Default: ci-dashboard-sync-unattached-block-volumes
  --schedule CRON                   Cron expression. Default: "20 3 * * *"
  --time-zone TZ                    CronJob timeZone. Default: Asia/Shanghai
  --db-secret NAME                  Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-db
  --aws-ebs-regions CSV             AWS regions to scan for available EBS volumes.
  --aws-ebs-account-id ID           AWS account id for volume snapshots. Optional; otherwise STS is used.
  --aws-owner-tag-keys CSV          Optional CI_DASHBOARD_AWS_EBS_OWNER_TAG_KEYS override.
  --aws-secret NAME                 Optional secret with AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY values.
  --aws-access-key-id-key NAME      Key inside AWS secret. Default: aws-access-key-id
  --aws-secret-access-key-key NAME  Key inside AWS secret. Default: aws-secret-access-key
  --aws-session-token-key NAME      Optional session token key inside AWS secret.
  --gcp-projects CSV                GCP projects to scan for Persistent Disk / Hyperdisk.
  --gcp-owner-label-keys CSV        Optional CI_DASHBOARD_GCP_BLOCK_VOLUME_OWNER_LABEL_KEYS override.
  --gcp-access-token-secret NAME    Optional secret containing CI_DASHBOARD_GCP_ACCESS_TOKEN.
  --gcp-access-token-key NAME       Key inside GCP token secret. Default: access-token
  --log-level LEVEL                 CI_DASHBOARD_LOG_LEVEL override. Default: INFO
  --image-pull-policy P             Image pull policy. Default: IfNotPresent
  --service-account NAME            Optional service account name.
  --ca-secret NAME                  Optional secret containing CA file for TIDB SSL.
  --ca-key NAME                     Key inside CA secret. Default: ca.crt
  --cpu-request VALUE               CPU request. Default: 250m
  --memory-request VALUE            Memory request. Default: 512Mi
  --cpu-limit VALUE                 CPU limit. Default: 1
  --memory-limit VALUE              Memory limit. Default: 1Gi
  --successful-history N            successfulJobsHistoryLimit. Default: 3
  --failed-history N                failedJobsHistoryLimit. Default: 3
  --backoff-limit N                 Job backoffLimit. Default: 1
  --active-deadline N               activeDeadlineSeconds. Default: 3600
  --starting-deadline N             startingDeadlineSeconds. Default: 1800
  --concurrency-policy VALUE        CronJob concurrencyPolicy. Default: Forbid
  --suspend true|false              Suspend CronJob. Default: false
  --help                            Show this help text.
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
    --aws-ebs-regions)
      aws_ebs_regions="${2:-}"
      shift 2
      ;;
    --aws-ebs-account-id)
      aws_ebs_account_id="${2:-}"
      shift 2
      ;;
    --aws-owner-tag-keys)
      aws_owner_tag_keys="${2:-}"
      shift 2
      ;;
    --aws-secret)
      aws_secret="${2:-}"
      shift 2
      ;;
    --aws-access-key-id-key)
      aws_access_key_id_key="${2:-}"
      shift 2
      ;;
    --aws-secret-access-key-key)
      aws_secret_access_key_key="${2:-}"
      shift 2
      ;;
    --aws-session-token-key)
      aws_session_token_key="${2:-}"
      shift 2
      ;;
    --gcp-projects)
      gcp_projects="${2:-}"
      shift 2
      ;;
    --gcp-owner-label-keys)
      gcp_owner_label_keys="${2:-}"
      shift 2
      ;;
    --gcp-access-token-secret)
      gcp_access_token_secret="${2:-}"
      shift 2
      ;;
    --gcp-access-token-key)
      gcp_access_token_key="${2:-}"
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

if [[ -z "${aws_ebs_regions}" && -z "${gcp_projects}" ]]; then
  echo "at least one of --aws-ebs-regions or --gcp-projects is required" >&2
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

aws_account_env_block=""
if [[ -n "${aws_ebs_account_id}" ]]; then
  aws_account_env_block=$(cat <<EOF
                - name: CI_DASHBOARD_AWS_EBS_ACCOUNT_ID
                  value: "${aws_ebs_account_id}"
EOF
)
fi

aws_owner_tag_keys_env_block=""
if [[ -n "${aws_owner_tag_keys}" ]]; then
  aws_owner_tag_keys_env_block=$(cat <<EOF
                - name: CI_DASHBOARD_AWS_EBS_OWNER_TAG_KEYS
                  value: "${aws_owner_tag_keys}"
EOF
)
fi

aws_secret_env_block=""
aws_session_token_env_block=""
if [[ -n "${aws_secret}" ]]; then
  aws_secret_env_block=$(cat <<EOF
                - name: AWS_ACCESS_KEY_ID
                  valueFrom:
                    secretKeyRef:
                      name: ${aws_secret}
                      key: ${aws_access_key_id_key}
                - name: AWS_SECRET_ACCESS_KEY
                  valueFrom:
                    secretKeyRef:
                      name: ${aws_secret}
                      key: ${aws_secret_access_key_key}
EOF
)
  if [[ -n "${aws_session_token_key}" ]]; then
    aws_session_token_env_block=$(cat <<EOF
                - name: AWS_SESSION_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: ${aws_secret}
                      key: ${aws_session_token_key}
EOF
)
  fi
fi

gcp_owner_label_keys_env_block=""
if [[ -n "${gcp_owner_label_keys}" ]]; then
  gcp_owner_label_keys_env_block=$(cat <<EOF
                - name: CI_DASHBOARD_GCP_BLOCK_VOLUME_OWNER_LABEL_KEYS
                  value: "${gcp_owner_label_keys}"
EOF
)
fi

gcp_access_token_env_block=""
if [[ -n "${gcp_access_token_secret}" ]]; then
  gcp_access_token_env_block=$(cat <<EOF
                - name: CI_DASHBOARD_GCP_ACCESS_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: ${gcp_access_token_secret}
                      key: ${gcp_access_token_key}
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
    app.kubernetes.io/name: ci-dashboard-sync-unattached-block-volumes
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
            app.kubernetes.io/name: ci-dashboard-sync-unattached-block-volumes
            app.kubernetes.io/part-of: ci-dashboard
        spec:
          restartPolicy: Never
${service_account_block}
          containers:
            - name: sync-unattached-block-volumes
              image: ${image}
              imagePullPolicy: ${image_pull_policy}
              args:
                - sync-unattached-block-volumes
              envFrom:
                - secretRef:
                    name: ${db_secret}
              env:
                - name: PYTHONUNBUFFERED
                  value: "1"
                - name: CI_DASHBOARD_LOG_LEVEL
                  value: ${log_level}
                - name: CI_DASHBOARD_AWS_EBS_REGIONS
                  value: "${aws_ebs_regions}"
${aws_account_env_block}
${aws_owner_tag_keys_env_block}
${aws_secret_env_block}
${aws_session_token_env_block}
                - name: CI_DASHBOARD_GCP_BLOCK_VOLUME_PROJECTS
                  value: "${gcp_projects}"
${gcp_owner_label_keys_env_block}
${gcp_access_token_env_block}
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
