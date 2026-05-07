#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest"
deployment_name="ci-dashboard-pod-watcher"
replicas="1"
db_secret="ci-dashboard-db"
gcp_project=""
pod_event_namespaces="prow-test-pods,jenkins-tidb,jenkins-tiflow"
cluster_name=""
location=""
watch_timeout_seconds="300"
retry_delay_seconds="5"
health_port="8081"
stale_after_seconds="720"
jenkins_prefix_cache_seconds="900"
db_batch_size="100"
db_retry_attempts="3"
db_retry_base_delay_ms="500"
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
Render a Kubernetes Deployment manifest for the CI Dashboard Pod watcher.

Usage:
  render_pod_watcher_deployment.sh [options]

Optional:
  --namespace NAME              Kubernetes namespace. Default: apps
  --image IMAGE                 Jobs image. Default: ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest
  --deployment-name NAME        Deployment name. Default: ci-dashboard-pod-watcher
  --replicas N                  Replica count. Default: 1
  --db-secret NAME              Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-db
  --gcp-project PROJECT         Source project stored in pod tables. Required.
  --pod-event-namespaces CSV    Watched namespaces. Default: prow-test-pods,jenkins-tidb,jenkins-tiflow
  --cluster-name NAME           Optional cluster name stored in pod tables.
  --location NAME               Optional cluster location stored in pod tables.
  --watch-timeout-seconds N     Kubernetes watch timeoutSeconds. Default: 300
  --retry-delay-seconds N       Delay before reconnect after a watch error. Default: 5
  --health-port N               HTTP health port exposed by watch-pods. Default: 8081
  --stale-after-seconds N       Mark watch streams unhealthy after no heartbeat. Default: 720
  --jenkins-prefix-cache-seconds N
                                Cache Jenkins pod-name prefix lookup for N seconds. Default: 900
  --db-batch-size N            DB write batch size for watcher persistence. Default: 100
  --db-retry-attempts N        Retry attempts for retryable DB write errors. Default: 3
  --db-retry-base-delay-ms N   Initial retry delay for DB write errors. Default: 500
  --log-level LEVEL             CI_DASHBOARD_LOG_LEVEL override. Default: INFO
  --image-pull-policy P         Image pull policy. Default: IfNotPresent
  --service-account NAME        Optional service account name.
  --ca-secret NAME              Optional secret containing CA file for TIDB SSL.
  --ca-key NAME                 Key inside CA secret. Default: ca.crt
  --cpu-request VALUE           CPU request. Default: 500m
  --memory-request VALUE        Memory request. Default: 1Gi
  --cpu-limit VALUE             CPU limit. Default: 1
  --memory-limit VALUE          Memory limit. Default: 2Gi
  --help                        Show this help text.
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
    --gcp-project)
      gcp_project="${2:-}"
      shift 2
      ;;
    --pod-event-namespaces)
      pod_event_namespaces="${2:-}"
      shift 2
      ;;
    --cluster-name)
      cluster_name="${2:-}"
      shift 2
      ;;
    --location)
      location="${2:-}"
      shift 2
      ;;
    --watch-timeout-seconds)
      watch_timeout_seconds="${2:-}"
      shift 2
      ;;
    --retry-delay-seconds)
      retry_delay_seconds="${2:-}"
      shift 2
      ;;
    --health-port)
      health_port="${2:-}"
      shift 2
      ;;
    --stale-after-seconds)
      stale_after_seconds="${2:-}"
      shift 2
      ;;
    --jenkins-prefix-cache-seconds)
      jenkins_prefix_cache_seconds="${2:-}"
      shift 2
      ;;
    --db-batch-size)
      db_batch_size="${2:-}"
      shift 2
      ;;
    --db-retry-attempts)
      db_retry_attempts="${2:-}"
      shift 2
      ;;
    --db-retry-base-delay-ms)
      db_retry_base_delay_ms="${2:-}"
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

cluster_env_block=""
if [[ -n "${cluster_name}" ]]; then
  cluster_env_block="${cluster_env_block}            - name: CI_DASHBOARD_KUBERNETES_CLUSTER_NAME
              value: ${cluster_name}
"
fi
if [[ -n "${location}" ]]; then
  cluster_env_block="${cluster_env_block}            - name: CI_DASHBOARD_KUBERNETES_LOCATION
              value: ${location}
"
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
    app.kubernetes.io/name: ci-dashboard-pod-watcher
    app.kubernetes.io/part-of: ci-dashboard
spec:
  replicas: ${replicas}
  selector:
    matchLabels:
      app.kubernetes.io/name: ci-dashboard-pod-watcher
      app.kubernetes.io/part-of: ci-dashboard
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ci-dashboard-pod-watcher
        app.kubernetes.io/part-of: ci-dashboard
    spec:
${service_account_block}
      restartPolicy: Always
      containers:
        - name: pod-watcher
          image: ${image}
          imagePullPolicy: ${image_pull_policy}
          args: ["watch-pods"]
          ports:
            - name: health
              containerPort: ${health_port}
          envFrom:
            - secretRef:
                name: ${db_secret}
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: CI_DASHBOARD_LOG_LEVEL
              value: ${log_level}
            - name: CI_DASHBOARD_GCP_PROJECT
              value: ${gcp_project}
            - name: CI_DASHBOARD_POD_EVENT_NAMESPACES
              value: ${pod_event_namespaces}
            - name: CI_DASHBOARD_POD_WATCH_TIMEOUT_SECONDS
              value: "${watch_timeout_seconds}"
            - name: CI_DASHBOARD_POD_WATCH_RETRY_DELAY_SECONDS
              value: "${retry_delay_seconds}"
            - name: CI_DASHBOARD_POD_WATCH_HEALTH_PORT
              value: "${health_port}"
            - name: CI_DASHBOARD_POD_WATCH_STALE_AFTER_SECONDS
              value: "${stale_after_seconds}"
            - name: CI_DASHBOARD_JENKINS_POD_NAME_PREFIX_CACHE_SECONDS
              value: "${jenkins_prefix_cache_seconds}"
            - name: CI_DASHBOARD_POD_WATCH_DB_BATCH_SIZE
              value: "${db_batch_size}"
            - name: CI_DASHBOARD_POD_WATCH_DB_RETRY_ATTEMPTS
              value: "${db_retry_attempts}"
            - name: CI_DASHBOARD_POD_WATCH_DB_RETRY_BASE_DELAY_MS
              value: "${db_retry_base_delay_ms}"
${cluster_env_block}${ca_env_block}
          startupProbe:
            httpGet:
              path: /livez
              port: health
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 18
          livenessProbe:
            httpGet:
              path: /livez
              port: health
            initialDelaySeconds: 30
            periodSeconds: 30
            timeoutSeconds: 3
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /readyz
              port: health
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
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
