#!/usr/bin/env bash
set -euo pipefail

namespace="apps"
db_secret="ci-dashboard-eq-prd-insight-db"
ca_secret="ci-dashboard-backfill-ca"
ca_key="ca.crt"

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd "${script_dir}/.." && pwd)
out_dir="${project_root}/.local"
env_file="${out_dir}/tidb.env"
ca_path="${out_dir}/tidb-ca.crt"

usage() {
  cat <<'EOF'
Prepare local TiDB environment files from Kubernetes secrets.

Usage:
  prepare_local_tidb_env.sh [options]

Options:
  --namespace NAME      Kubernetes namespace. Default: apps
  --db-secret NAME      Secret containing TIDB_* or CI_DASHBOARD_DB_URL. Default: ci-dashboard-eq-prd-insight-db
  --ca-secret NAME      Secret containing TiDB CA file. Default: ci-dashboard-backfill-ca
  --ca-key NAME         Key inside the CA secret. Default: ca.crt
  --out-dir PATH        Output directory. Default: ci-dashboard/.local
  --help                Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      namespace="${2:-}"
      shift 2
      ;;
    --db-secret)
      db_secret="${2:-}"
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
    --out-dir)
      out_dir="${2:-}"
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

mkdir -p "${out_dir}"
env_file="${out_dir}/tidb.env"
ca_path="${out_dir}/tidb-ca.crt"

read_secret_value() {
  local secret_name="$1"
  local key="$2"
  kubectl -n "${namespace}" get secret "${secret_name}" -o "jsonpath={.data.${key}}" 2>/dev/null
}

decode_base64() {
  python3 -c '
import base64
import sys

raw = sys.stdin.read().strip()
if raw:
    sys.stdout.write(base64.b64decode(raw).decode("utf-8"))
'
}

write_env_line() {
  local key="$1"
  local value="$2"
  printf '%s=%q\n' "${key}" "${value}" >> "${env_file}"
}

url_raw=$(read_secret_value "${db_secret}" "CI_DASHBOARD_DB_URL")
host_raw=$(read_secret_value "${db_secret}" "TIDB_HOST")
port_raw=$(read_secret_value "${db_secret}" "TIDB_PORT")
user_raw=$(read_secret_value "${db_secret}" "TIDB_USER")
password_raw=$(read_secret_value "${db_secret}" "TIDB_PASSWORD")
database_raw=$(read_secret_value "${db_secret}" "TIDB_DB")
ssl_ca_raw=$(read_secret_value "${db_secret}" "TIDB_SSL_CA")

: > "${env_file}"

if [[ -n "${url_raw}" ]]; then
  write_env_line "CI_DASHBOARD_DB_URL" "$(printf '%s' "${url_raw}" | decode_base64)"
else
  if [[ -z "${host_raw}" || -z "${port_raw}" || -z "${user_raw}" || -z "${password_raw}" || -z "${database_raw}" ]]; then
    echo "secret ${namespace}/${db_secret} is missing required TiDB connection keys" >&2
    exit 1
  fi

  write_env_line "TIDB_HOST" "$(printf '%s' "${host_raw}" | decode_base64)"
  write_env_line "TIDB_PORT" "$(printf '%s' "${port_raw}" | decode_base64)"
  write_env_line "TIDB_USER" "$(printf '%s' "${user_raw}" | decode_base64)"
  write_env_line "TIDB_PASSWORD" "$(printf '%s' "${password_raw}" | decode_base64)"
  write_env_line "TIDB_DB" "$(printf '%s' "${database_raw}" | decode_base64)"
fi

decoded_ssl_ca=""
if [[ -n "${ssl_ca_raw}" ]]; then
  decoded_ssl_ca="$(printf '%s' "${ssl_ca_raw}" | decode_base64)"
fi

if [[ -n "${decoded_ssl_ca}" && -f "${decoded_ssl_ca}" ]]; then
  write_env_line "TIDB_SSL_CA" "${decoded_ssl_ca}"
elif [[ -n "${ca_secret}" ]]; then
  ca_raw=$(read_secret_value "${ca_secret}" "${ca_key//./\\.}")
  if [[ -z "${ca_raw}" ]]; then
    echo "secret ${namespace}/${ca_secret} is missing key ${ca_key}" >&2
    exit 1
  fi
  printf '%s' "${ca_raw}" | python3 -c '
import base64
import sys

sys.stdout.buffer.write(base64.b64decode(sys.stdin.read().strip()))
' > "${ca_path}"
  write_env_line "TIDB_SSL_CA" "${ca_path}"
elif [[ -n "${decoded_ssl_ca}" ]]; then
  write_env_line "TIDB_SSL_CA" "${decoded_ssl_ca}"
fi

echo "wrote ${env_file}"
if [[ -f "${ca_path}" ]]; then
  echo "wrote ${ca_path}"
fi
