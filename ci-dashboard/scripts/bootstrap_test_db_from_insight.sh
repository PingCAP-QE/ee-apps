#!/usr/bin/env bash
set -euo pipefail

source_env_file=""
start_date="2026-03-16"
target_host=""
target_port="4000"
target_user=""
target_password=""
target_db="test"
target_ssl_ca="/etc/ssl/cert.pem"

usage() {
  cat <<'EOF'
Bootstrap the target TiDB database for ci-dashboard by:
1. creating the 3 read-only source tables in the target database,
2. loading a filtered source-table subset from the source insight cluster, and
3. creating the ci_* tables owned by ci-dashboard.

Usage:
  bootstrap_test_db_from_insight.sh \
    --source-env-file PATH \
    --target-host HOST \
    --target-user USER \
    --target-password PASSWORD \
    [options]

Required:
  --source-env-file PATH   Env file containing source TIDB_* values.
  --target-host HOST       Target TiDB host.
  --target-user USER       Target TiDB user.
  --target-password PASS   Target TiDB password.

Optional:
  --start-date DATE        Inclusive lower bound in YYYY-MM-DD. Default: 2026-03-16
  --target-port PORT       Target TiDB port. Default: 4000
  --target-db NAME         Target database name. Default: test
  --target-ssl-ca PATH     Target CA bundle path. Default: /etc/ssl/cert.pem
  --help                   Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-env-file)
      source_env_file="${2:-}"
      shift 2
      ;;
    --start-date)
      start_date="${2:-}"
      shift 2
      ;;
    --target-host)
      target_host="${2:-}"
      shift 2
      ;;
    --target-port)
      target_port="${2:-}"
      shift 2
      ;;
    --target-user)
      target_user="${2:-}"
      shift 2
      ;;
    --target-password)
      target_password="${2:-}"
      shift 2
      ;;
    --target-db)
      target_db="${2:-}"
      shift 2
      ;;
    --target-ssl-ca)
      target_ssl_ca="${2:-}"
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

if [[ -z "${source_env_file}" || -z "${target_host}" || -z "${target_user}" || -z "${target_password}" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -f "${source_env_file}" ]]; then
  echo "source env file not found: ${source_env_file}" >&2
  exit 1
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)

set -a
. "${source_env_file}"
set +a

required_source_vars=(TIDB_HOST TIDB_PORT TIDB_USER TIDB_PASSWORD TIDB_DB TIDB_SSL_CA)
for key in "${required_source_vars[@]}"; do
  if [[ -z "${!key:-}" ]]; then
    echo "missing source variable in ${source_env_file}: ${key}" >&2
    exit 1
  fi
done

source_db="${TIDB_DB}"
source_mysql=(/opt/homebrew/opt/mysql-client/bin/mysql --connect-timeout=10 --host="${TIDB_HOST}" --port="${TIDB_PORT}" --user="${TIDB_USER}" --ssl-ca="${TIDB_SSL_CA}")
source_dump=(/opt/homebrew/opt/mysql-client/bin/mysqldump --host="${TIDB_HOST}" --port="${TIDB_PORT}" --user="${TIDB_USER}" --ssl-ca="${TIDB_SSL_CA}" --single-transaction --skip-lock-tables --skip-triggers --compact)
target_mysql=(/opt/homebrew/opt/mysql-client/bin/mysql --connect-timeout=10 --host="${target_host}" --port="${target_port}" --user="${target_user}" --ssl-ca="${target_ssl_ca}")

prow_where="startTime >= '${start_date} 00:00:00'"
case_where="report_time >= '${start_date} 00:00:00'"
ticket_where="((type = 'pull' AND EXISTS (SELECT 1 FROM ${source_db}.prow_jobs p WHERE p.startTime >= '${start_date} 00:00:00' AND p.pull IS NOT NULL AND p.org IS NOT NULL AND p.repo IS NOT NULL AND CONCAT(p.org, '/', p.repo) = github_tickets.repo AND p.pull = github_tickets.number)) OR (type = 'issue' AND repo = 'pingcap/tidb' AND title LIKE 'Flaky test:%'))"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

run_target_sql() {
  local sql="$1"
  MYSQL_PWD="${target_password}" "${target_mysql[@]}" -e "${sql}"
}

run_source_scalar() {
  local sql="$1"
  MYSQL_PWD="${TIDB_PASSWORD}" "${source_mysql[@]}" -N -e "${sql}"
}

import_schema_from_source() {
  local table_name="$1"
  log "Creating target schema for ${table_name}"
  set +e
  MYSQL_PWD="${TIDB_PASSWORD}" "${source_dump[@]}" --no-data "${source_db}" "${table_name}" 2>/dev/null \
    | sed 's/^CREATE TABLE `/CREATE TABLE IF NOT EXISTS `/1' \
    | MYSQL_PWD="${target_password}" "${target_mysql[@]}" "${target_db}"
  local statuses=("${PIPESTATUS[@]}")
  set -e
  if [[ "${statuses[0]}" != "0" && "${statuses[0]}" != "2" ]]; then
    echo "schema dump failed for ${table_name} with exit code ${statuses[0]}" >&2
    exit 1
  fi
  if [[ "${statuses[2]}" != "0" ]]; then
    echo "target schema import failed for ${table_name} with exit code ${statuses[2]}" >&2
    exit 1
  fi
}

import_data_from_source() {
  local table_name="$1"
  local where_clause="$2"
  log "Importing ${table_name} rows into ${target_db}"
  set +e
  MYSQL_PWD="${TIDB_PASSWORD}" "${source_dump[@]}" --replace --no-create-info --where="${where_clause}" "${source_db}" "${table_name}" 2>/dev/null \
    | MYSQL_PWD="${target_password}" "${target_mysql[@]}" "${target_db}"
  local statuses=("${PIPESTATUS[@]}")
  set -e
  if [[ "${statuses[0]}" != "0" && "${statuses[0]}" != "2" ]]; then
    echo "data dump failed for ${table_name} with exit code ${statuses[0]}" >&2
    exit 1
  fi
  if [[ "${statuses[1]}" != "0" ]]; then
    echo "target data import failed for ${table_name} with exit code ${statuses[1]}" >&2
    exit 1
  fi
}

apply_target_sql_file() {
  local file_path="$1"
  log "Applying $(basename "${file_path}") to ${target_db}"
  MYSQL_PWD="${target_password}" "${target_mysql[@]}" "${target_db}" < "${file_path}"
}

log "Ensuring target database ${target_db} exists"
run_target_sql "CREATE DATABASE IF NOT EXISTS \`${target_db}\`"

import_schema_from_source "prow_jobs"
import_schema_from_source "github_tickets"
import_schema_from_source "problem_case_runs"

apply_target_sql_file "${repo_root}/sql/003_create_ci_job_state.sql"
apply_target_sql_file "${repo_root}/sql/001_create_ci_l1_builds.sql"
apply_target_sql_file "${repo_root}/sql/002_create_ci_l1_pr_events.sql"
apply_target_sql_file "${repo_root}/sql/004_create_ci_l1_flaky_issues.sql"

import_data_from_source "prow_jobs" "${prow_where}"
import_data_from_source "problem_case_runs" "${case_where}"
import_data_from_source "github_tickets" "${ticket_where}"

log "Target row counts after bootstrap"
MYSQL_PWD="${target_password}" "${target_mysql[@]}" -N "${target_db}" -e "
SELECT 'prow_jobs', COUNT(*) FROM prow_jobs
UNION ALL
SELECT 'problem_case_runs', COUNT(*) FROM problem_case_runs
UNION ALL
SELECT 'github_tickets', COUNT(*) FROM github_tickets
UNION ALL
SELECT 'ci_job_state', COUNT(*) FROM ci_job_state
UNION ALL
SELECT 'ci_l1_builds', COUNT(*) FROM ci_l1_builds
UNION ALL
SELECT 'ci_l1_pr_events', COUNT(*) FROM ci_l1_pr_events
UNION ALL
SELECT 'ci_l1_flaky_issues', COUNT(*) FROM ci_l1_flaky_issues;
"

log "Source-side reference counts for the selected window"
run_source_scalar "
SELECT 'prow_jobs', COUNT(*) FROM ${source_db}.prow_jobs WHERE ${prow_where}
UNION ALL
SELECT 'problem_case_runs', COUNT(*) FROM ${source_db}.problem_case_runs WHERE ${case_where}
UNION ALL
SELECT 'github_tickets', COUNT(*) FROM ${source_db}.github_tickets WHERE ${ticket_where};
"
