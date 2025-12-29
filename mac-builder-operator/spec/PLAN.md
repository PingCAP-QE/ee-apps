# Implementation Roadmap

You have completed the API design and the core macOS Agent build logic. The remaining work focuses on GC, packaging, and production readiness.

## ✅ Phase 1: Core Build Capability (Completed)

- [x] Initialize Kubebuilder project.
- [x] Define MacBuild CRD (Nested Spec).
- [x] Implement macOS Agent Watch mechanism.
- [x] Implement nativeBuildJob: Git operations, Script generation, Local Shell execution, ORAS upload.
- [x] Implement status writing and concurrency locking (WorkerID).

## ✅ Phase 2: Garbage Collection (GC) Controller (Completed)

Tasks:

- [x] Create New Controller:
  - [x] Create `macbuild_gc_controller.go` under internal/controller/.
  - [x] Define the `MacBuildGCReconciler` struct.
- [x] Implement RBAC:
  - [x] Requires delete permission: `// +kubebuilder:rbac:groups=build.pingcap.com,resources=macbuilds,verbs=get;list;watch;delete`
- [x] Implement Reconcile Logic:
  - [x] Fetch the `MacBuild` object.
  - [x] Check if `Status.Phase is Succeeded` or Failed.
  - [x] Check if `Spec.TtlSecondsAfterFinished` is set.
  - [x] Check if `Status.CompletionTime` is set.
  - [x] Decision: `time.Now().After(CompletionTime.Add(TTL))`.
  - [x] Action: `r.Delete(ctx, &macBuild)`.
  - [x] Requeue: If not expired, use `ctrl.Result{RequeueAfter: remainingTime}`.

- [x] Registration:
  - [x] Register this new controller in main.go.

## ✅ Phase 3: Build & Deployment Configuration (Completed)

Tasks:

- [x] Multi-mode Startup (main.go):
  - [x] Modify `main.go` to add command-line flags to control which controller starts.
  - [x] `--enable-agent`: Start Agent Reconciler (default: false).
  - [x] `--enable-gc`: Start GC Reconciler (default: true).
  - Goal: Single binary. Runs in Agent mode on Mac, and GC mode in
