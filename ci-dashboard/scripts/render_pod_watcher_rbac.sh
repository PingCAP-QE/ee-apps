#!/usr/bin/env bash
set -euo pipefail

name="ci-dashboard-pod-watcher"
service_account_namespace="apps"
service_account_name="ci-dashboard"
target_namespaces="prow-test-pods,jenkins-tidb,jenkins-tiflow"

usage() {
  cat <<'EOF'
Render Kubernetes RBAC for the CI Dashboard Pod watcher.

The rendered RBAC grants get/list/watch on pods and events only in the target
namespaces by binding a shared ClusterRole with namespace-scoped RoleBindings.

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

while [[ $# -gt 0 ]]; do
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

if [[ -z "${name}" ]]; then
  echo "--name must not be empty" >&2
  exit 1
fi
if [[ -z "${service_account_namespace}" ]]; then
  echo "--service-account-namespace must not be empty" >&2
  exit 1
fi
if [[ -z "${service_account_name}" ]]; then
  echo "--service-account-name must not be empty" >&2
  exit 1
fi
if [[ -z "${target_namespaces}" ]]; then
  echo "--target-namespaces must not be empty" >&2
  exit 1
fi

cat <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${name}
  labels:
    app.kubernetes.io/name: ci-dashboard-pod-watcher
    app.kubernetes.io/part-of: ci-dashboard
rules:
  - apiGroups: [""]
    resources: ["pods", "events"]
    verbs: ["get", "list", "watch"]
EOF

IFS=',' read -r -a namespaces <<< "${target_namespaces}"
for raw_namespace in "${namespaces[@]}"; do
  namespace="$(echo "${raw_namespace}" | xargs)"
  if [[ -z "${namespace}" ]]; then
    continue
  fi
  cat <<EOF
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${name}
  namespace: ${namespace}
  labels:
    app.kubernetes.io/name: ci-dashboard-pod-watcher
    app.kubernetes.io/part-of: ci-dashboard
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${name}
subjects:
  - kind: ServiceAccount
    name: ${service_account_name}
    namespace: ${service_account_namespace}
EOF
done
