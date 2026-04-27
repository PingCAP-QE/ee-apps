#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest"
deployment_name="ci-dashboard-jenkins-worker"
replicas="1"
db_secret="ci-dashboard-db"
kafka_bootstrap_servers=""
kafka_topic="jenkins-event"
kafka_group_id="ci-dashboard-v3-jenkins-worker"
poll_timeout_ms="1000"
finished_event_type="dev.cdevents.pipelinerun.finished.0.1.0"
log_level="INFO"
image_pull_policy="IfNotPresent"
service_account=""
ca_secret=""
ca_key="ca.crt"
cpu_request="500m"
memory_request="1Gi"
cpu_limit="1"
memory_limit="2Gi"

usage() {
  cat <<'EOF'
Render a Kubernetes Deployment manifest for the V3 Jenkins event worker.

Usage:
  render_jenkins_worker_deployment.sh [options]

Optional:
  --namespace NAME               Kubernetes namespace. Default: apps
  --image IMAGE                  Jobs image. Default: ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest
  --deployment-name NAME         Deployment name. Default: ci-dashboard-jenkins-worker
  --replicas N                   Replica count. Default: 1
  --db-secret NAME               Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-db
  --kafka-bootstrap-servers CSV  Kafka bootstrap servers. Required.
  --kafka-topic NAME             Kafka topic. Default: jenkins-event
  --kafka-group-id NAME          Kafka consumer group. Default: ci-dashboard-v3-jenkins-worker
  --poll-timeout-ms N            Kafka poll timeout in milliseconds. Default: 1000
  --finished-event-type TYPE     Accepted Jenkins finished-event type. Default: dev.cdevents.pipelinerun.finished.0.1.0
  --log-level LEVEL              CI_DASHBOARD_LOG_LEVEL override. Default: INFO
  --image-pull-policy P          Image pull policy. Default: IfNotPresent
  --service-account NAME         Optional service account name.
  --ca-secret NAME               Optional secret containing CA file for TIDB SSL.
  --ca-key NAME                  Key inside CA secret. Default: ca.crt
  --cpu-request VALUE            CPU request. Default: 500m
  --memory-request VALUE         Memory request. Default: 1Gi
  --cpu-limit VALUE              CPU limit. Default: 1
  --memory-limit VALUE           Memory limit. Default: 2Gi
  --help                         Show this help text.
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
    --deployment-name)
      deployment_name="${2:-}"
      shift 2
      ;;
    --replicas)
      replicas="${2:-}"
      shift 2
      ;;
    --db-secret)
      db_secret="${2:-}"
      shift 2
      ;;
    --kafka-bootstrap-servers)
      kafka_bootstrap_servers="${2:-}"
      shift 2
      ;;
    --kafka-topic)
      kafka_topic="${2:-}"
      shift 2
      ;;
    --kafka-group-id)
      kafka_group_id="${2:-}"
      shift 2
      ;;
    --poll-timeout-ms)
      poll_timeout_ms="${2:-}"
      shift 2
      ;;
    --finished-event-type)
      finished_event_type="${2:-}"
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

if [[ -z "${kafka_bootstrap_servers}" ]]; then
  echo "--kafka-bootstrap-servers is required" >&2
  exit 1
fi

service_account_block=""
if [[ -n "${service_account}" ]]; then
  service_account_block=$(cat <<EOF
      serviceAccountName: ${service_account}
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
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${deployment_name}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/name: ci-dashboard-jenkins-worker
    app.kubernetes.io/part-of: ci-dashboard
spec:
  replicas: ${replicas}
  selector:
    matchLabels:
      app.kubernetes.io/name: ci-dashboard-jenkins-worker
      app.kubernetes.io/part-of: ci-dashboard
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ci-dashboard-jenkins-worker
        app.kubernetes.io/part-of: ci-dashboard
    spec:
${service_account_block}
      restartPolicy: Always
      containers:
        - name: jenkins-worker
          image: ${image}
          imagePullPolicy: ${image_pull_policy}
          args: ["consume-jenkins-events"]
          envFrom:
            - secretRef:
                name: ${db_secret}
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: CI_DASHBOARD_LOG_LEVEL
              value: ${log_level}
            - name: CI_DASHBOARD_KAFKA_BOOTSTRAP_SERVERS
              value: ${kafka_bootstrap_servers}
            - name: CI_DASHBOARD_KAFKA_JENKINS_EVENTS_TOPIC
              value: ${kafka_topic}
            - name: CI_DASHBOARD_KAFKA_JENKINS_GROUP_ID
              value: ${kafka_group_id}
            - name: CI_DASHBOARD_KAFKA_POLL_TIMEOUT_MS
              value: "${poll_timeout_ms}"
            - name: CI_DASHBOARD_JENKINS_FINISHED_EVENT_TYPE
              value: ${finished_event_type}
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
