# Design Doc: Kubernetes Native macOS Build Operator

## 1. Project Overview
This project aims to bring macOS platform build tasks into the Kubernetes declarative management system. By leveraging a Custom Resource (MacBuild) and an Agent running directly on macOS physical machines, we achieve a "native" build process that eliminates dependencies on SSH and external orchestrators like Tekton/Boskos.

## 2. Core Architecture
The system consists of two main components:

1. macOS Agent (External Controller):
   - Deployment: Runs on macOS physical machines or VMs (Outside the K8s cluster).
   - Responsibilities: Proactively watches the K8s API, claims tasks in Pending state, executes local operations (git clone, script generation, compilation, oras push), and updates the CRD Status in real-time.
   - Communication: Connects to the K8s API Server via Kubeconfig or ServiceAccount Token (Outbound connection).

2. GC Controller (Cluster Internal):
   - Deployment: Runs inside a Pod within the K8s cluster (Standard Deployment).
   - Responsibilities: Monitors completed tasks (Succeeded or Failed) and automatically deletes the CRD resources based on a TTL strategy to free up Etcd storage.

## 3. API Definition (CRD)
- Group: build.tibuild.pingcap.net
- Version: v1alpha1
- Kind: MacBuild

### Spec Structure

```go
type MacBuildSpec struct {
    // Source defines the code to be built
    Source SourceSpec `json:"source"`

    // Build defines the parameters for the build process
    Build BuildSpec `json:"build"`

    // Artifacts defines where and how to publish the build output
    Artifacts ArtifactsSpec `json:"artifacts"`

    // Lifecycle management (GC)
    // Seconds to retain the build resource after it has finished (succeeded or failed).
    // +optional
    TtlSecondsAfterFinished *int32 `json:"ttlSecondsAfterFinished,omitempty"`
}

type SourceSpec struct {
    GitRepository string  `json:"gitRepository"`
    GitRef        string  `json:"gitRef"`
    GitSha        *string `json:"gitSha,omitempty"`     // Optional, overrides GitRef
    GitRefspec    *string `json:"gitRefspec,omitempty"` // Optional, for PRs
}

type BuildSpec struct {
		Component     string  `json:"component"`
    Version string `json:"version"`
    Arch    string `json:"arch,omitempty"`    // default: amd64
    Profile string `json:"profile,omitempty"` // default: release
}

type ArtifactsSpec struct {
    Push     bool   `json:"push,omitempty"`     // default: false
    Registry string `json:"registry,omitempty"` // default: hub.pingcap.net
}
```

### Status Structure

```go
type MacBuildStatus struct {
    Phase string `json:"phase,omitempty"` // Pending, Building, Succeeded, Failed

    // Task Assignment & Execution Info
    WorkerID       *string      `json:"workerID,omitempty"`
    StartTime      *metav1.Time `json:"startTime,omitempty"`
    CompletionTime *metav1.Time `json:"completionTime,omitempty"` // Used for GC

    // Results
    CommitHash          *string `json:"commitHash,omitempty"`
    PushedArtifactsYaml *string `json:"pushedArtifactsYaml,omitempty"` // Final artifact list
    Message             *string `json:"message,omitempty"`             // Error logs/messages
}
````

## 4. State Machine (Agent Logic)
1. Pending:
   - Agent discovers a new task.
   - Action: Updates status.phase = Building, status.workerID = <hostname>, status.startTime = Now().

2. Building:
   - Agent checks if workerID matches its own ID.
   - Action: Executes nativeBuildJob (Setup Workspace -> Clone -> Gen Script -> Build -> Push).

   - Result:
     - Success: Updates status.phase = Succeeded, status.pushedArtifactsYaml, status.completionTime.
     - Failure: Updates status.phase = Failed, status.message, status.completionTime.

4. Succeeded / Failed:
   - Terminal states. The Agent stops processing; waiting for GC.
