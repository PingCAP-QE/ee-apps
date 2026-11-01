# macOS build worker

Installer for a macOS worker that runs the `mac-builder-operator` build agent.
The agent registers a cluster-scoped `MacAgent`, heartbeats its inventory, and
executes only the `MacBuild` objects the cluster scheduler assigns to it.

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

Run `install.sh --help`. Common ones: `--kubeconfig`, `--worker-arch`,
`--agent-name` (deprecated alias: `--worker-name`), `--labels`,
`--capacity`, `--heartbeat-interval`, `--namespace`, `--service`
(LaunchAgent), `--run` (foreground).

## Registration and scheduling

The agent's `MacAgent` name is derived from `IOPlatformUUID` (serial number and
hostname are fallbacks), so reinstalling or renaming the host does not create a
duplicate machine object. `--agent-name` overrides the derived name.

`--labels key=value,...` adds scheduling labels such as
`tibuild.pingcap.net/pool=mac-mini`; `--capacity` sets concurrent builds
(default 1). `kubectl get macagents` shows each machine's phase, IP, OS,
architecture, CPU/memory, load, disk and active builds; `kubectl describe
macagent <name>` shows the full status.

For maintenance, cordon first and wait for active builds to finish:

```bash
kubectl patch macagent <name> --type=merge -p '{"spec":{"unschedulable":true}}'
kubectl patch macagent <name> --type=merge -p '{"spec":{"drain":true}}'
```

The agent persists the in-flight build in
`~/Library/Application Support/macbuild-agent/state.json` (override with
`--state-file`). On restart it reruns a build that was in Preparing/Building and
fails a build that was already Publishing, avoiding a duplicate publish.

## Private cluster access

The agent needs a kubeconfig for the cluster (no gcloud dependency). See the
worker ServiceAccount/RBAC in the ee-ops chart and mint a token-based kubeconfig,
then pass it via `--kubeconfig`.

## Private repositories (git credentials)

Builds of private repositories (e.g. `pingcap-inc/tici`) need git credentials.
A build references a Kubernetes Secret in the MacBuild namespace through
`spec.source.gitSecretRef` (Tekton: the `git-secret-ref` CustomRun param) — the
same Secret shapes the Tekton `pingcap-git-clone` task consumes via its
`basic-auth`/`ssh-directory` workspaces. The agent materializes the credentials
**per build only** (removed right after the checkout; never written to logs,
artifacts, or anywhere outside the build workspace).

Supported Secret shapes:

- **HTTPS (PAT) — `kubernetes.io/basic-auth`** with `username` + `password`
  (`password` may be replaced by a `token` key). Common values:
  `username: x-access-token`, `password: <GitHub PAT>`. A packed
  `.git-credentials` data key is also accepted verbatim.
- **SSH — `kubernetes.io/ssh-auth`** with `ssh-privatekey` (plus optional
  `known_hosts`), or the Tekton `ssh-directory` style key files
  (`id_ed25519`/`id_rsa`/`known_hosts`/`config`, ...).

The credentials are also used when initializing git submodules. Builds without
`source.gitSecretRef` clone anonymously (public repositories).

Example:

```bash
# One credentials Secret per namespace (or one per repository).
kubectl -n ee-cd create secret generic github-git-credentials \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=x-access-token \
  --from-literal=password="$GITHUB_PAT"

# Reference it from the build (MacBuild CR, or the `git-secret-ref` param when
# driven by a Tekton CustomRun):
#   spec:
#     source:
#       gitRepository: https://github.com/pingcap-inc/tici.git
#       gitRef: main
#       gitSecretRef: github-git-credentials
```

The worker's ServiceAccount needs `get` on Secrets in the MacBuild namespace to
read the referenced one; the chart's worker Role grants exactly that (it is
namespace-scoped to the MacBuild namespace). Credential values are never
logged; failures report only the Secret name and the reason.
