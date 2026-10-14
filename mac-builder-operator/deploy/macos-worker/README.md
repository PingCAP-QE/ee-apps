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

## Private repositories (git credentials)

Builds of private repositories (e.g. `pingcap-inc/tici`) need git credentials.
The agent takes them from a Kubernetes Secret — the same idea as binding a
`basic-auth`/`ssh-directory` Secret to the Tekton `pingcap-git-clone` task — and
materializes them **per build only** (removed right after the checkout;
never written to logs, artifacts, or anywhere outside the build workspace).

Supported Secret shapes:

- **HTTPS (PAT) — `kubernetes.io/basic-auth`** with `username` + `password`
  (`password` may be replaced by a `token` key). Common values:
  `username: x-access-token`, `password: <GitHub PAT>`. A packed
  `.git-credentials` data key is also accepted verbatim.
- **SSH — `kubernetes.io/ssh-auth`** with `ssh-privatekey` (plus optional
  `known_hosts`), or the Tekton `ssh-directory` style key files
  (`id_ed25519`/`id_rsa`/`known_hosts`/`config`, ...).

The credentials are also used when initializing git submodules.

Resolution order for each build:

1. `spec.source.gitSecretRef` (a Secret name in the MacBuild's namespace), or
   the `git-secret-ref` CustomRun param when driven by Tekton;
2. the agent's default Secret name, set with `--default-git-credentials-secret`;
3. none — the clone is anonymous (public repositories only).

Example:

```bash
# One credentials Secret per namespace (or one per repository).
kubectl -n ee-cd create secret generic github-git-credentials \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=x-access-token \
  --from-literal=password="$GITHUB_PAT"

# Then install/start the agent with the namespace-wide default (install.sh
# passes it through to the agent as a flag):
#   install.sh --service --default-git-credentials-secret=github-git-credentials
```

The worker's ServiceAccount needs `get` on the git credentials Secret in the
MacBuild namespace. For chart-managed workers, list the Secret name(s) in
`worker.gitCredentialsSecrets` — the chart grants access by `resourceNames`, so
the worker can only read exactly those Secrets. Credential values are never
logged; failures report only the Secret name and the reason.
