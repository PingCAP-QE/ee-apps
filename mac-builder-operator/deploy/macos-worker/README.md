# macOS build worker

Installer for a macOS worker that runs the `mac-builder-operator` build agent
(the agent claims `MacBuild` objects and builds natively on the mac).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/PingCAP-QE/ee-apps/<ref>/mac-builder-operator/deploy/macos-worker/install.sh | bash
# or, from a checkout:
mac-builder-operator/deploy/macos-worker/install.sh [OPTIONS]
```

What it does (idempotent):

1. Checks **Xcode Command Line Tools** (detect only — the version cannot be pinned via the CLI; it is a machine-image prerequisite).
2. Installs + activates **mise**.
3. Applies the **worker-global** toolset from `PingCAP-QE/artifacts`
   (`packages/macos/bootstrap/mise.toml`: gomplate/yq/jq/deno/oras) to the global mise config.
4. Builds + installs the **agent** binary (`~/.local/bin/macbuild-agent`).
5. Starts it — foreground (`--run`) or as a **LaunchAgent** (`--service`); otherwise prints the start command.

Per-component/version tools (e.g. `go`) are provisioned by the agent **per build**
from the component's `macos.tools` — not by this installer.

## Options

Run `install.sh --help`. Common ones: `--kubeconfig`, `--worker-arch`, `--worker-name`,
`--namespace`, `--service` (LaunchAgent), `--run` (foreground).

## Private cluster access

The agent needs a kubeconfig for the cluster (no gcloud dependency). See the
worker ServiceAccount/RBAC in the ee-ops chart and mint a token-based kubeconfig,
then pass it via `--kubeconfig`.
