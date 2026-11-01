#!/usr/bin/env bash
# macOS worker installer for the mac-builder-operator build agent.
#
# Run on a macOS worker to check prerequisites, install everything the agent
# needs, and (optionally) start the agent as a LaunchAgent. Idempotent.
#
#   curl -fsSL <raw-url>/deploy/macos-worker/install.sh | bash
#
# It performs, in order:
#   1. Xcode Command Line Tools — detect only (version cannot be pinned via CLI).
#   2. mise — install + activate.
#   3. Worker-global toolchain — apply the artifacts-declared tools
#      (PingCAP-QE/artifacts packages/macos/bootstrap/mise.toml) to the global
#      mise config.
#   4. Build + install the agent binary.
#   5. Start it (foreground, or as a LaunchAgent with --service).
#
# Per-component/version tools (e.g. `go` for builds) are provisioned by the agent
# itself, per build; this installer only sets up the worker.
set -euo pipefail

# ---- defaults ---------------------------------------------------------------
EEAPPS_REPO="${EEAPPS_REPO:-https://github.com/PingCAP-QE/ee-apps.git}"
EEAPPS_REF="${EEAPPS_REF:-main}"
ARTIFACTS_REPO="${ARTIFACTS_REPO:-https://github.com/PingCAP-QE/artifacts.git}"
ARTIFACTS_REF="${ARTIFACTS_REF:-main}"
PREFIX="${PREFIX:-$HOME/.local/bin}"
WORKER_NAME="${WORKER_NAME:-}"
AGENT_NAME="${AGENT_NAME:-$WORKER_NAME}"
AGENT_LABELS="${AGENT_LABELS:-}"
AGENT_CAPACITY="${AGENT_CAPACITY:-1}"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-20s}"
MIN_FREE_DISK_BYTES="${MIN_FREE_DISK_BYTES:-21474836480}"
DISK_CHECK_ATTEMPTS="${DISK_CHECK_ATTEMPTS:-2}"
DISK_CHECK_RETRY_INTERVAL="${DISK_CHECK_RETRY_INTERVAL:-100ms}"
SHUTDOWN_GRACE="${SHUTDOWN_GRACE:-10m}"
WORKSPACE_MAX_AGE="${WORKSPACE_MAX_AGE:-168h}"
AGENT_VERSION="${AGENT_VERSION:-$EEAPPS_REF}"
STATE_FILE="${STATE_FILE:-}"
LOG_UPLOAD="${LOG_UPLOAD:-true}"
LOG_SERVICE="${LOG_SERVICE:-mac-builder-operator-logs}"
LOG_SERVICE_NAMESPACE="${LOG_SERVICE_NAMESPACE:-ee-cd}"
LOG_SERVICE_PORT="${LOG_SERVICE_PORT:-8080}"
LOG_MAX_BUILD_BYTES="${LOG_MAX_BUILD_BYTES:-104857600}"
WORKER_ARCH="${WORKER_ARCH:-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/;s/arm64/arm64/')}"
MACBUILD_NAMESPACE="${MACBUILD_NAMESPACE:-ee-cd}"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/config}"
AGENT_BUILD_GO="${AGENT_BUILD_GO:-1.25}"
INSTALL_SERVICE=false
RUN_FOREGROUND=false
SERVICE_LABEL="net.pingcap.macbuild-agent"

log() { printf '%s [install] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

[[ -n "$WORKER_NAME" && -z "$AGENT_NAME" ]] && AGENT_NAME="$WORKER_NAME"

usage() {
  cat >&2 <<EOF
Usage: install.sh [OPTIONS]

  --kubeconfig PATH       kubeconfig for the cluster (default: \$KUBECONFIG or ~/.kube/config)
  --agent-name NAME       MacAgent name override (default: derived from IOPlatformUUID)
  --worker-name NAME      Deprecated: use --agent-name
  --worker-arch ARCH      amd64|arm64 (default: host)
  --labels LIST           comma-separated key=value scheduling labels
  --capacity N            concurrent builds (default: 1)
  --heartbeat-interval D  heartbeat interval (default: 20s)
  --min-free-disk-bytes N minimum free disk before starting a build (default: 21474836480)
  --disk-check-attempts N disk measurements before failing a build (default: 2)
  --disk-check-retry-interval D delay between disk measurements (default: 100ms)
  --shutdown-grace D      wait for active builds after SIGTERM (default: 10m)
  --workspace-max-age D   remove build workspaces older than this (default: 168h)
  --log-upload BOOL       upload build logs (default: true)
  --log-service NAME      log server Service (default: mac-builder-operator-logs)
  --log-service-namespace NS log server namespace (default: ee-cd)
  --log-service-port N    log server Service port (default: 8080)
  --log-max-build-bytes N max bytes per build log (default: 104857600)
  --agent-version VERSION version reported in MacAgent status (default: --eeapps-ref)
  --state-file PATH       in-flight build state file (default: per-user config dir)
  --namespace NS          namespace where MacBuilds live (default: ee-cd)
  --prefix DIR            install prefix for the agent binary (default: ~/.local/bin)
  --eeapps-ref REF        ee-apps ref to build the agent from (default: main)
  --artifacts-ref REF     artifacts ref for the worker-global toolset (default: main)
  --service               install + start a LaunchAgent (${SERVICE_LABEL})
  --run                   build everything, then run the agent in the foreground
  -h|--help               show this help
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kubeconfig)     KUBECONFIG_PATH="$2"; shift 2 ;;
    --agent-name)     AGENT_NAME="$2"; shift 2 ;;
    --worker-name)    AGENT_NAME="$2"; log "--worker-name is deprecated; use --agent-name"; shift 2 ;;
    --worker-arch)    WORKER_ARCH="$2"; shift 2 ;;
    --labels)         AGENT_LABELS="$2"; shift 2 ;;
    --capacity)       AGENT_CAPACITY="$2"; shift 2 ;;
    --heartbeat-interval) HEARTBEAT_INTERVAL="$2"; shift 2 ;;
    --min-free-disk-bytes) MIN_FREE_DISK_BYTES="$2"; shift 2 ;;
    --disk-check-attempts) DISK_CHECK_ATTEMPTS="$2"; shift 2 ;;
    --disk-check-retry-interval) DISK_CHECK_RETRY_INTERVAL="$2"; shift 2 ;;
    --shutdown-grace) SHUTDOWN_GRACE="$2"; shift 2 ;;
    --workspace-max-age) WORKSPACE_MAX_AGE="$2"; shift 2 ;;
    --log-upload) LOG_UPLOAD="$2"; shift 2 ;;
    --log-service) LOG_SERVICE="$2"; shift 2 ;;
    --log-service-namespace) LOG_SERVICE_NAMESPACE="$2"; shift 2 ;;
    --log-service-port) LOG_SERVICE_PORT="$2"; shift 2 ;;
    --log-max-build-bytes) LOG_MAX_BUILD_BYTES="$2"; shift 2 ;;
    --agent-version)  AGENT_VERSION="$2"; shift 2 ;;
    --state-file)     STATE_FILE="$2"; shift 2 ;;
    --namespace)      MACBUILD_NAMESPACE="$2"; shift 2 ;;
    --prefix)         PREFIX="$2"; shift 2 ;;
    --eeapps-ref)     EEAPPS_REF="$2"; shift 2 ;;
    --artifacts-ref)  ARTIFACTS_REF="$2"; shift 2 ;;
    --service)        INSTALL_SERVICE=true; shift ;;
    --run)            RUN_FOREGROUND=true; shift ;;
    -h|--help)        usage 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

case "$WORKER_ARCH" in amd64|arm64) ;; *) die "unsupported --worker-arch: $WORKER_ARCH";; esac

# ---- 1) Xcode Command Line Tools (detect only) ------------------------------
if xcode-select -p >/dev/null 2>&1; then
  log "Xcode CLT present: $(xcode-select -p)"
else
  die "Xcode Command Line Tools are missing. Install them (version cannot be pinned): xcode-select --install"
fi

# ---- 2) mise ---------------------------------------------------------------
if ! command -v mise >/dev/null 2>&1; then
  log "Installing mise..."
  curl -fsSL https://mise.run | sh
fi
export PATH="$HOME/.local/bin:$PREFIX:$PATH"
command -v mise >/dev/null 2>&1 || die "mise not found on PATH after install"
MISE_BIN="$(command -v mise)"
log "mise: $(mise --version)"

# ---- 3) worker-global toolchain from artifacts -----------------------------
workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

log "Fetching artifacts@${ARTIFACTS_REF} for the worker-global toolset..."
git clone --depth 1 --branch "$ARTIFACTS_REF" "$ARTIFACTS_REPO" "$workdir/artifacts"
mise_toml="$workdir/artifacts/packages/macos/bootstrap/mise.toml"
[[ -f "$mise_toml" ]] || die "missing $mise_toml in artifacts@${ARTIFACTS_REF}"
mkdir -p "$HOME/.config/mise/conf.d"
cp "$mise_toml" "$HOME/.config/mise/conf.d/macos-bootstrap.toml"
mise install
mise reshim || true

# ---- 4) build + install the agent ------------------------------------------
log "Ensuring a Go toolchain to build the agent (go@${AGENT_BUILD_GO})..."
mise use -g "go@${AGENT_BUILD_GO}"
log "Fetching ee-apps@${EEAPPS_REF} to build the agent..."
git clone --depth 1 --branch "$EEAPPS_REF" "$EEAPPS_REPO" "$workdir/ee-apps"
mkdir -p "$PREFIX"
( cd "$workdir/ee-apps/mac-builder-operator" && mise exec -- go build -o "$PREFIX/macbuild-agent" ./cmd )
log "Installed agent: $PREFIX/macbuild-agent"

[[ -f "$KUBECONFIG_PATH" ]] || log "WARNING: kubeconfig not found at $KUBECONFIG_PATH (the agent will fail to reach the cluster)"

agent_args=(--enable-agent --enable-gc=false --enable-customrun=false \
  --macbuild-namespace="$MACBUILD_NAMESPACE" --worker-arch="$WORKER_ARCH" \
  --capacity="$AGENT_CAPACITY" --heartbeat-interval="$HEARTBEAT_INTERVAL" \
  --min-free-disk-bytes="$MIN_FREE_DISK_BYTES" \
  --disk-check-attempts="$DISK_CHECK_ATTEMPTS" \
  --disk-check-retry-interval="$DISK_CHECK_RETRY_INTERVAL" \
  --shutdown-grace="$SHUTDOWN_GRACE" \
  --workspace-max-age="$WORKSPACE_MAX_AGE" --log-upload="$LOG_UPLOAD" \
  --log-service="$LOG_SERVICE" --log-service-namespace="$LOG_SERVICE_NAMESPACE" \
  --log-service-port="$LOG_SERVICE_PORT" --log-max-build-bytes="$LOG_MAX_BUILD_BYTES")
if [[ -n "$AGENT_NAME" ]]; then
  agent_args+=(--agent-name="$AGENT_NAME")
fi
if [[ -n "$AGENT_LABELS" ]]; then
  agent_args+=(--labels="$AGENT_LABELS")
fi
if [[ -n "$AGENT_VERSION" ]]; then
  agent_args+=(--agent-version="$AGENT_VERSION")
fi
if [[ -n "$STATE_FILE" ]]; then
  agent_args+=(--state-file="$STATE_FILE")
fi

# ---- 5) start --------------------------------------------------------------
if [[ "$INSTALL_SERVICE" == true ]]; then
  plist="$HOME/Library/LaunchAgents/${SERVICE_LABEL}.plist"
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${SERVICE_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>${MISE_BIN}</string>
    <string>exec</string>
    <string>--</string>
    <string>${PREFIX}/macbuild-agent</string>
$(
  for a in "${agent_args[@]}"; do printf '    <string>%s</string>\n' "$a"; done
)
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>${HOME}/.local/share/mise/shims:${HOME}/.local/bin:${PREFIX}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>KUBECONFIG</key><string>${KUBECONFIG_PATH}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${HOME}/Library/Logs/macbuild-agent.log</string>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/macbuild-agent.err.log</string>
</dict></plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load -w "$plist"
  log "Installed + started LaunchAgent ${SERVICE_LABEL} (logs: ~/Library/Logs/macbuild-agent*.log)"
elif [[ "$RUN_FOREGROUND" == true ]]; then
  log "Starting agent in the foreground..."
  # Keep Homebrew on PATH for foreground runs too: /opt/homebrew for arm64,
  # /usr/local for x86_64 (some build scripts call `brew --prefix ...`).
  exec env KUBECONFIG="$KUBECONFIG_PATH" PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" \
    "$MISE_BIN" exec -- "$PREFIX/macbuild-agent" "${agent_args[@]}"
else
  log "Done. Start the agent with:"
  log "  KUBECONFIG=$KUBECONFIG_PATH mise exec -- $PREFIX/macbuild-agent ${agent_args[*]}"
  log "or re-run this installer with --service (LaunchAgent) or --run (foreground)."
fi
