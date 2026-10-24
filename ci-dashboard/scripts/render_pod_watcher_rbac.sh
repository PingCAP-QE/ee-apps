#!/usr/bin/env bash
set -euo pipefail

name="ci-dashboard-pod-watcher"
service_account_namespace="apps"
service_account_name="ci-dashboard"
target_namespaces="prow-test-pods,jenkins-tidb,jenkins-tiflow"

usage() {
  cat <<'EOF'
Render Kubernetes RBAC for the CI Dashboard Pod watcher.

The rendered RBAC grants get/list/watch on pods and core/v1 events only in the target
namespaces by binding a shared ClusterRole with namespace-scoped RoleBindings.
PVC reads used by GCP sync-pods are separate; watch-pods does not need them.

Usage:
  render_pod_watcher_rbac.sh [options]

Optional:
  --name NAME                     RBAC resource name. Default: ci-dashboard-pod-watcher
  --service-account-namespace NS  ServiceAccount namespace. Default: apps
  --service-account-name NAME     ServiceAccount name. Default: ci-dashboard
  --target-namespaces CSV         Namespaces to watch. Default: prow-test-pods,jenkins-tidb,jenkins-tiflow
  --help                          Show this help text.
EOF
}

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

# Keep this standalone Bash renderer independent of Python/YAML dependencies.
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
    --name)
      name="${2:-}"
      shift 2
      ;;
    --service-account-namespace)
      service_account_namespace="${2:-}"
      shift 2
      ;;
    --service-account-name)
      service_account_name="${2:-}"
      shift 2
      ;;
    --target-namespaces)
      target_namespaces="${2:-}"
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

# Parse and validate every namespace before emitting even the shared role.
for option in name service_account_namespace service_account_name target_namespaces; do
  [[ -n "${!option}" && ! "${!option}" =~ ^[[:space:]]*$ ]] || fail "$option must not be blank"
done
[[ "$target_namespaces" != *[$'\001'-$'\037']* ]] || fail "target namespaces contain control characters"
[[ "$target_namespaces" != ,* && "$target_namespaces" != *, ]] || fail "target namespaces must not be empty"
IFS=',' read -r -a raw_namespaces <<< "$target_namespaces"
namespaces=()
for raw_namespace in "${raw_namespaces[@]}"; do
  namespace="${raw_namespace#"${raw_namespace%%[![:space:]]*}"}"
  namespace="${namespace%"${namespace##*[![:space:]]}"}"
  [[ ${#namespace} -le 63 && "$namespace" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || \
    fail "invalid target namespace: $raw_namespace"
  namespaces+=("$namespace")
done

cat <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: $(yaml_quote "$name")
  labels:
    app.kubernetes.io/name: ci-dashboard-pod-watcher
    app.kubernetes.io/part-of: ci-dashboard
rules:
  - apiGroups: [""]
    resources: ["pods", "events"]
    verbs: ["get", "list", "watch"]
EOF

for namespace in "${namespaces[@]}"; do
  cat <<EOF
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: $(yaml_quote "$name")
  namespace: $(yaml_quote "$namespace")
  labels:
    app.kubernetes.io/name: ci-dashboard-pod-watcher
    app.kubernetes.io/part-of: ci-dashboard
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: $(yaml_quote "$name")
subjects:
  - kind: ServiceAccount
    name: $(yaml_quote "$service_account_name")
    namespace: $(yaml_quote "$service_account_namespace")
EOF
done
