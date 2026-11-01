# Kubernetes Native macOS Build Operator

## Description

This project aims to bring macOS platform build tasks into the Kubernetes declarative management system. By leveraging a Custom Resource (MacBuild) and an Agent running directly on macOS physical machines, we achieve a "native" build process that eliminates dependencies on SSH and external orchestrators like Tekton/Boskos.

## Getting Started

### Running Controllers

Cluster mode (scheduler + MacAgent inventory + GC + Tekton CustomRun):

```sh
go run ./cmd/main.go --enable-gc --enable-customrun --enable-macagent --enable-scheduler \
  --macbuild-namespace=ee-cd
```

Mac worker agent:

```sh
go run ./cmd/main.go --enable-agent --enable-gc=false --enable-customrun=false \
  --macbuild-namespace=ee-cd \
  --worker-arch="$(go env GOARCH)" --capacity=1 \
  --heartbeat-interval=20s --build-timeout=24h
```

The agent derives its `MacAgent` name from the machine's `IOPlatformUUID` (serial
number and hostname are fallbacks). `--agent-name` overrides it; `--worker-name`
is a deprecated alias. `--labels key=value,...` adds scheduling labels such as
`tibuild.pingcap.net/pool=mac-mini`.

`--build-timeout` bounds how long a job may remain active before it is marked
`Failed`. `--build-poll-interval` is retained for compatibility; agents no longer
poll jobs owned by other workers. The agent pins the external `artifacts.git`
build-script source through `--artifacts-repo-revision` plus
`--artifacts-repo-commit`; branch refs such as `main` are rejected.

The worker persists the in-flight build in
`~/Library/Application Support/macbuild-agent/state.json` (override with
`--state-file`) so a restart can decide whether to rerun or fail the build.
`--shutdown-grace` (default 10m) lets an active build finish after SIGTERM;
stale `macbuild-*` workspaces in the temp dir are removed after
`--workspace-max-age` (default 7d).

### Scheduling and MacAgent

- The cluster scheduler assigns `Pending` builds to a `MacAgent` by architecture,
  `spec.workerSelector` labels, capacity and least normalized load. Agents execute
  only builds whose `status.assignment.agentName` matches their own name.
- `MacAgent` is cluster-scoped. `kubectl get macagents` shows phase, architecture,
  host, IP, active/capacity builds and online duration; `kubectl describe macagent`
  shows OS, CPU/memory, load averages, disk, conditions and assigned builds.
- Heartbeat is 20s. After 60s without a heartbeat the agent is `Offline`; active
  builds keep a 10m grace period (`spec.offlineGracePeriod`), then fail with
  `AgentLost`. Reconnecting within the grace window lets the build continue.
- `spec.unschedulable=true` cordons an agent; `spec.drain=true` stops new
  assignments and waits for active builds to finish.
- Low disk space sets the `DiskPressure` condition and removes the agent from
  scheduling. A build that starts below `--min-free-disk-bytes` fails with an
  `insufficient disk` message before cloning.

### MacBuild API Notes

- `.spec.build.arch` is optional and defaults to `amd64`.
- The scheduler sets `.status.phase=Assigned`, `.status.assignment`, and
  `.status.logs.command`; the agent sets `.status.workerID`, `.status.workerArch`,
  `.status.phaseMessage`, and `.status.phaseHistory[]` when it starts the build.
- `.spec.workerSelector` requires all entries to match the `MacAgent`'s labels.
- Phase history entries capture the phase name, timestamp, and a short message for
  each transition.

Example `MacBuild`:

```yaml
apiVersion: tibuild.pingcap.net/v1alpha1
kind: MacBuild
metadata:
  name: macbuild-sample
spec:
  source:
    gitRepository: https://github.com/pingcap/tidb.git
    gitRef: main
  build:
    component: tidb
    version: nightly
    arch: arm64
  artifacts:
    push: false
  workerSelector:
    tibuild.pingcap.net/pool: mac-mini
```

### Prerequisites
- go version v1.25.0+
- docker version 17.03+.
- kubectl version v1.11.3+.
- Access to a Kubernetes v1.11.3+ cluster.

### To Deploy on the cluster
**Build and push your image to the location specified by `IMG`:**

```sh
make docker-build docker-push IMG=<some-registry>/mac-builder-operator:tag
```

**NOTE:** This image ought to be published in the personal registry you specified.
And it is required to have access to pull the image from the working environment.
Make sure you have the proper permission to the registry if the above commands don’t work.

**Install the CRDs into the cluster:**

```sh
make install
```

**Deploy the Manager to the cluster with the image specified by `IMG`:**

```sh
make deploy IMG=<some-registry>/mac-builder-operator:tag
```

> **NOTE**: If you encounter RBAC errors, you may need to grant yourself cluster-admin
privileges or be logged in as admin.

**Create instances of your solution**
You can apply the samples (examples) from the config/sample:

```sh
kubectl apply -k config/samples/
```

>**NOTE**: Ensure that the samples has default values to test it out.

### To Uninstall
**Delete the instances (CRs) from the cluster:**

```sh
kubectl delete -k config/samples/
```

**Delete the APIs(CRDs) from the cluster:**

```sh
make uninstall
```

**UnDeploy the controller from the cluster:**

```sh
make undeploy
```

## Project Distribution

Following the options to release and provide this solution to the users.

### By providing a bundle with all YAML files

1. Build the installer for the image built and published in the registry:

```sh
make build-installer IMG=<some-registry>/mac-builder-operator:tag
```

**NOTE:** The makefile target mentioned above generates an 'install.yaml'
file in the dist directory. This file contains all the resources built
with Kustomize, which are necessary to install this project without its
dependencies.

2. Using the installer

Users can just run 'kubectl apply -f <URL for YAML BUNDLE>' to install
the project, i.e.:

```sh
kubectl apply -f https://raw.githubusercontent.com/<org>/mac-builder-operator/<tag or branch>/dist/install.yaml
```

### By providing a Helm Chart

1. Build the chart using the optional helm plugin

```sh
kubebuilder edit --plugins=helm/v1-alpha
```

2. See that a chart was generated under 'dist/chart', and users
can obtain this solution from there.

**NOTE:** If you change the project, you need to update the Helm Chart
using the same command above to sync the latest changes. Furthermore,
if you create webhooks, you need to use the above command with
the '--force' flag and manually ensure that any custom configuration
previously added to 'dist/chart/values.yaml' or 'dist/chart/manager/manager.yaml'
is manually re-applied afterwards.

## Contributing

**NOTE:** Run `make help` for more information on all potential `make` targets

### Testing

- `make test` runs the default unit and envtest suites.
- `go test -tags=manual -v -run TestManualNativeBuildJob ./internal/controller` runs the networked manual build test.

More information can be found via the [Kubebuilder Documentation](https://book.kubebuilder.io/introduction.html)

## License

Copyright 2025.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
