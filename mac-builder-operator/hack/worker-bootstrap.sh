#!/usr/bin/env bash
# Idempotent bootstrap for a macOS worker that runs the mac-builder-operator agent.
#
# Responsibilities (see conductor track macbuild_toolchain_decl_20261007):
#   1. Xcode Command Line Tools — DETECT ONLY. Their version cannot be pinned from
#      the CLI (xcode-select --install installs whatever the OS offers); a specific
#      version is a machine-image prerequisite, so we only verify presence.
#   2. mise — install (official installer) if missing, then activate.
#   3. Generic (worker-global) toolchain — apply the artifacts-declared tools
#      (packages/macos/bootstrap/mise.toml) to the worker's GLOBAL mise config and
#      install them, so they are available everywhere on the worker.
#
# Per-component/version tools (e.g. `go`) are NOT installed here; the agent
# provisions those per build from the component's `macos.tools`.
#
# Usage:
#   worker-bootstrap.sh [ARTIFACTS_DIR]
# Environment:
#   ARTIFACTS_DIR   Directory of a PingCAP-QE/artifacts checkout. Defaults to the
#                   first positional argument. If unset, step 3 is skipped.
#   MISE_BIN        Path to the mise binary. Default: $HOME/.local/bin/mise.
set -euo pipefail

log() { printf '%s [bootstrap] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

ARTIFACTS_DIR="${1:-${ARTIFACTS_DIR:-}}"
MISE_BIN="${MISE_BIN:-$HOME/.local/bin/mise}"
MISE_CONFIG_DIR="${MISE_CONFIG_DIR:-$HOME/.config/mise}"

# 1) Xcode Command Line Tools — detect only.
if xcode-select -p >/dev/null 2>&1; then
  log "Xcode CLT present: $(xcode-select -p)"
else
  log "ERROR: Xcode Command Line Tools are missing (required for CGO/clang)."
  log "Install them manually — the version cannot be pinned from the CLI:"
  log "  xcode-select --install"
  exit 1
fi

# 2) mise — install if missing.
if command -v mise >/dev/null 2>&1; then
  log "mise present: $(mise --version)"
else
  log "Installing mise via the official installer..."
  curl -fsSL https://mise.run | sh
fi
export PATH="$MISE_BIN:$HOME/.local/bin:$PATH"
if ! command -v mise >/dev/null 2>&1; then
  log "ERROR: mise not found on PATH after install (looked for $MISE_BIN)."
  exit 1
fi

# 3) Generic worker-global toolchain from artifacts -> global mise config.
if [[ -z "$ARTIFACTS_DIR" ]]; then
  log "ARTIFACTS_DIR not set; skipping generic toolchain install."
else
  MISE_TOML="$ARTIFACTS_DIR/packages/macos/bootstrap/mise.toml"
  if [[ ! -f "$MISE_TOML" ]]; then
    log "ERROR: $MISE_TOML not found."
    exit 1
  fi
  mkdir -p "$MISE_CONFIG_DIR/conf.d"
  # conf.d fragments are loaded globally, so the declared [tools] become
  # worker-global — no TOML parsing needed here.
  cp "$MISE_TOML" "$MISE_CONFIG_DIR/conf.d/macos-bootstrap.toml"
  log "Applied $MISE_TOML -> $MISE_CONFIG_DIR/conf.d/macos-bootstrap.toml"
  mise install
  mise reshim || true
fi

log "Bootstrap complete."
log "Ensure the agent (and its child processes) see mise's shims on PATH:"
log "  - interactive shells: add 'eval \"\$(mise activate zsh)\"' to ~/.zshrc"
log "  - services/headless: prepend \$HOME/.local/share/mise/shims to PATH"
