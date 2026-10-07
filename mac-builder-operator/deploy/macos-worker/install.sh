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
WORKER_NAME="${WORKER_NAME:-$(hostname)}"
WORKER_ARCH="${WORKER_ARCH:-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/;s/arm64/arm64/')}"
MACBUILD_NAMESPACE="${MACBUILD_NAMESPACE:-ee-cd}"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/config}"
AGENT_BUILD_GO="${AGENT_BUILD_GO:-1.25}"
INSTALL_SERVICE=false
RUN_FOREGROUND=false
SERVICE_LABEL="net.pingcap.macbuild-agent"

log() { printf '%s [install] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

usage() {
  cat >&2 <<EOF
Usage: install.sh [OPTIONS]

  --kubeconfig PATH       kubeconfig for the cluster (default: \$KUBECONFIG or ~/.kube/config)
  --worker-name NAME      worker identity (default: hostname)
  --worker-arch ARCH      amd64|arm64 (default: host)
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
    --worker-name)    WORKER_NAME="$2"; shift 2 ;;
    --worker-arch)    WORKER_ARCH="$2"; shift 2 ;;
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
  --macbuild-namespace="$MACBUILD_NAMESPACE" --worker-name="$WORKER_NAME" --worker-arch="$WORKER_ARCH")

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
    <string>${PREFIX}/macbuild-agent</string>
$(
  for a in "${agent_args[@]}"; do printf '    <string>%s</string>\n' "$a"; done
)
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>${HOME}/.local/bin:${PREFIX}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
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
  exec env KUBECONFIG="$KUBECONFIG_PATH" PATH="$HOME/.local/bin:$PREFIX:$PATH" "$PREFIX/macbuild-agent" "${agent_args[@]}"
else
  log "Done. Start the agent with:"
  log "  KUBECONFIG=$KUBECONFIG_PATH mise exec -- $PREFIX/macbuild-agent ${agent_args[*]}"
  log "or re-run this installer with --service (LaunchAgent) or --run (foreground)."
fi
