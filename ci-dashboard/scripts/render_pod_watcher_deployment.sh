#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
image="ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs:latest"
deployment_name="ci-dashboard-pod-watcher"
replicas="1"
db_secret="ci-dashboard-db"
gcp_project=""
source_project=""
source_project_set=false
gcp_project_set=false
cloud_phase="GCP"
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
db_retry_max_delay_ms="5000"
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
  --replicas N                  Must be 1 (no leader election). Default: 1
  --db-secret NAME              Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-db
  --source-project SOURCE       Source identity stored in pod tables. Required (or --gcp-project).
  --gcp-project PROJECT          GCP-only compatibility alias for --source-project.
  --cloud-phase GCP|TENCENT      Source cloud. Default: GCP
  --pod-event-namespaces CSV    Watched namespaces. Default: prow-test-pods,jenkins-tidb,jenkins-tiflow
  --cluster-name NAME           Cluster display name; required for Tencent.
  --location NAME               Cluster location; required and must match Tencent source region.
  --watch-timeout-seconds N     Kubernetes watch timeoutSeconds. Default: 300
  --retry-delay-seconds N       Delay before reconnect after a watch error. Default: 5
  --health-port N               HTTP health port exposed by watch-pods. Default: 8081
  --stale-after-seconds N       Mark watch streams unhealthy after no heartbeat. Default: 720
  --jenkins-prefix-cache-seconds N
                                Cache Jenkins pod-name prefix lookup for N seconds. Default: 900
  --db-batch-size N            DB write batch size for watcher persistence. Default: 100
  --db-retry-attempts N        Retry attempts for retryable DB write errors. Default: 3
  --db-retry-base-delay-ms N   Initial retry delay for DB write errors. Default: 500
  --db-retry-max-delay-ms N    Maximum retry delay for DB write errors. Default: 5000
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

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

is_nonblank() {
  local value="$1"
  value="${value//[$'\034'-$'\037']/}"
  value="${value//$'\302\205'/}"
  value="${value//$'\302\240'/}"
  value="${value//$'\342\200\207'/}"
  value="${value//$'\342\200\257'/}"
  [[ -n "$value" && ! "$value" =~ ^[[:space:]]*$ ]]
}

# YAML double-quoted scalars preserve punctuation, types and line breaks literally.
yaml_quote() {
  local value="$1" code octal char escaped
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  # Bash 3 printf reads only the first byte of a Unicode character: build the
  # forbidden YAML control characters explicitly, leaving other UTF-8 intact.
  for ((code = 1; code <= 159; code++)); do
    ((code < 32 || code >= 127)) || continue
    printf -v octal '%03o' "$code"
    if ((code > 127)); then
      printf -v char '%b' "\\302\\$octal"
    else
      printf -v char '%b' "\\$octal"
    fi
    printf -v escaped '\\u%04x' "$code"
    value="${value//$char/$escaped}"
  done
  value="${value//$'\342\200\250'/\\u2028}"
  value="${value//$'\342\200\251'/\\u2029}"
  value="${value//$'\357\277\276'/\\ufffe}"
  value="${value//$'\357\277\277'/\\uffff}"
  printf '"%s"' "$value"
}

while [[ $# -gt 0 ]]; do
  if [[ "$1" != --help && "$1" != -h ]]; then
    [[ $# -ge 2 && "$2" != --* ]] || fail "missing value for $1"
  fi
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
    --source-project)
      source_project="${2:-}"
      source_project_set=true
      shift 2
      ;;
    --gcp-project)
      gcp_project="${2:-}"
      gcp_project_set=true
      shift 2
      ;;
    --cloud-phase)
      cloud_phase="${2:-}"
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
    --db-retry-max-delay-ms)
      db_retry_max_delay_ms="${2:-}"
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

# Validate everything before writing any manifest (including optional blocks).
if $source_project_set && $gcp_project_set && [[ "$source_project" != "$gcp_project" ]]; then
  fail "--source-project and --gcp-project conflict"
fi
if ! $source_project_set; then
  source_project="$gcp_project"
fi
[[ -n "$source_project" && ! "$source_project" =~ ^[[:space:]]|[[:space:]]$ ]] || \
  fail "--source-project is required and must not have surrounding whitespace"
# Python str.strip also recognizes these whitespace characters, which are not
# consistently included in POSIX [:space:] across the supported Bash locales.
case "$source_project" in
  $'\302\205'*|*$'\302\205'|$'\302\240'*|*$'\302\240'|\
  $'\342\200\207'*|*$'\342\200\207'|$'\342\200\257'*|*$'\342\200\257')
    fail "source identity must not have surrounding whitespace"
    ;;
esac
[[ ${#source_project} -le 128 ]] || fail "source identity exceeds 128 characters"
[[ "$source_project" != *[$'\001'-$'\037']* ]] || fail "source identity contains control characters"
[[ "$cloud_phase" == GCP || "$cloud_phase" == TENCENT ]] || fail "--cloud-phase must be GCP or TENCENT"
source_cloud="GCP"
if [[ "$source_project" == [tT][eE][nN][cC][eE][nN][tT]:* ]]; then
  [[ "$source_project" =~ ^tencent:([a-z][a-z0-9]*(-[a-z0-9]+)+):(cls-[a-z0-9]+)$ ]] || \
    fail "Tencent source must have the form tencent:<region>:cls-<id> (lowercase)"
  source_region="${BASH_REMATCH[1]}"
  watch_job_name="ci-watch-pods:${source_project}"
  [[ ${#watch_job_name} -le 64 ]] || fail "Tencent watcher job-state key exceeds 64 characters"
  ! $gcp_project_set || fail "--gcp-project cannot supply a Tencent source; use --source-project"
  source_cloud="TENCENT"
  is_nonblank "$cluster_name" || fail "Tencent requires --cluster-name"
  is_nonblank "$location" || fail "Tencent requires --location"
  [[ "$source_region" == "$location" ]] || fail "Tencent source region and --location disagree"
fi
[[ "$cloud_phase" == "$source_cloud" ]] || fail "source identity and --cloud-phase disagree"
[[ "$replicas" == 1 ]] || fail "--replicas must be 1 until leader election is supported"
for option in namespace image deployment_name db_secret pod_event_namespaces log_level \
  cpu_request memory_request cpu_limit memory_limit; do
  is_nonblank "${!option}" || fail "$option must not be blank"
done
[[ "$image_pull_policy" == Always || "$image_pull_policy" == IfNotPresent || "$image_pull_policy" == Never ]] || \
  fail "--image-pull-policy must be Always, IfNotPresent or Never"
for option in watch_timeout_seconds retry_delay_seconds health_port stale_after_seconds \
  db_batch_size db_retry_attempts db_retry_base_delay_ms db_retry_max_delay_ms; do
  [[ "${!option}" =~ ^[1-9][0-9]*$ ]] || fail "$option must be a positive integer"
done
[[ "$jenkins_prefix_cache_seconds" =~ ^(0|[1-9][0-9]*)$ ]] || \
  fail "jenkins_prefix_cache_seconds must be a nonnegative integer"
[[ ${#health_port} -le 5 && "$health_port" -le 65535 ]] || fail "--health-port must be in 1..65535"
if [[ -n "$ca_secret" ]]; then
  [[ "$ca_key" =~ ^[a-zA-Z0-9._-]+$ && "$ca_key" != . && "$ca_key" != .. ]] || fail "invalid --ca-key"
fi
[[ "$pod_event_namespaces" != *[$'\001'-$'\037']* ]] || fail "watched namespaces contain control characters"
IFS=',' read -r -a namespaces <<< "$pod_event_namespaces"
[[ "$pod_event_namespaces" != ,* && "$pod_event_namespaces" != *, ]] || fail "watched namespaces must not be empty"
for raw_namespace in "${namespaces[@]}"; do
  target_namespace="${raw_namespace#"${raw_namespace%%[![:space:]]*}"}"
  target_namespace="${target_namespace%"${target_namespace##*[![:space:]]}"}"
  [[ ${#target_namespace} -le 63 && "$target_namespace" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || \
    fail "invalid watched namespace: $raw_namespace"
done

legacy_gcp_env_block=""
if [[ "$cloud_phase" == GCP ]]; then
  legacy_gcp_env_block="            - name: CI_DASHBOARD_GCP_PROJECT
              value: $(yaml_quote "$source_project")
"
fi

service_account_block=""
if [[ -n "${service_account}" ]]; then
  service_account_block=$(cat <<EOF
      serviceAccountName: $(yaml_quote "$service_account")
EOF
)
fi

cluster_env_block=""
if [[ -n "${cluster_name}" ]]; then
  cluster_env_block="${cluster_env_block}            - name: CI_DASHBOARD_KUBERNETES_CLUSTER_NAME
              value: $(yaml_quote "$cluster_name")
"
fi
if [[ -n "${location}" ]]; then
  cluster_env_block="${cluster_env_block}            - name: CI_DASHBOARD_KUBERNETES_LOCATION
              value: $(yaml_quote "$location")
"
fi

ca_env_block=""
ca_mount_block=""
ca_volume_block=""
if [[ -n "${ca_secret}" ]]; then
  ca_env_block=$(cat <<EOF
            - name: TIDB_SSL_CA
              value: $(yaml_quote "/var/run/ci-dashboard/ssl/${ca_key}")
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
            secretName: $(yaml_quote "$ca_secret")
            items:
              - key: $(yaml_quote "$ca_key")
                path: $(yaml_quote "$ca_key")
EOF
)
fi

cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $(yaml_quote "$deployment_name")
  namespace: $(yaml_quote "$namespace")
  labels:
    app.kubernetes.io/name: ci-dashboard-pod-watcher
    app.kubernetes.io/part-of: ci-dashboard
spec:
  replicas: 1
  strategy:
    type: Recreate
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
          image: $(yaml_quote "$image")
          imagePullPolicy: $(yaml_quote "$image_pull_policy")
          args: ["watch-pods"]
          ports:
            - name: health
              containerPort: ${health_port}
          envFrom:
            - secretRef:
                name: $(yaml_quote "$db_secret")
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: CI_DASHBOARD_LOG_LEVEL
              value: $(yaml_quote "$log_level")
            - name: CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT
              value: $(yaml_quote "$source_project")
            - name: CI_DASHBOARD_POD_WATCH_CLOUD_PHASE
              value: $(yaml_quote "$cloud_phase")
${legacy_gcp_env_block}            - name: CI_DASHBOARD_POD_EVENT_NAMESPACES
              value: $(yaml_quote "$pod_event_namespaces")
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
            - name: CI_DASHBOARD_POD_WATCH_DB_RETRY_MAX_DELAY_MS
              value: "${db_retry_max_delay_ms}"
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
              cpu: $(yaml_quote "$cpu_request")
              memory: $(yaml_quote "$memory_request")
            limits:
              cpu: $(yaml_quote "$cpu_limit")
              memory: $(yaml_quote "$memory_limit")
EOF
if [[ -n "$ca_mount_block" ]]; then
  printf '%s\n' "$ca_mount_block" "$ca_volume_block"
fi
