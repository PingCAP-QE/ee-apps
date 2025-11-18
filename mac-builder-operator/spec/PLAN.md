# Implementation Roadmap

You have completed the API design and the core macOS Agent build logic. The remaining work focuses on GC, packaging, and production readiness.

## ✅ Phase 1: Core Build Capability (Completed)

- [x] Initialize Kubebuilder project.
- [x] Define MacBuild CRD (Nested Spec).
- [x] Implement macOS Agent Watch mechanism.
- [x] Implement nativeBuildJob: Git operations, Script generation, Local Shell execution, ORAS upload.
- [x] Implement status writing and concurrency locking (WorkerID).

## 🚀 Phase 2: Garbage Collection (GC) Controller (Next Step)

Tasks:

- [ ] Create New Controller:
  - [ ] Create `macbuild_gc_controller.go` under internal/controller/.
  - [ ] Define the `MacBuildGCReconciler` struct.
- [ ] Implement RBAC:
  - [ ] Requires delete permission: `// +kubebuilder:rbac:groups=build.pingcap.com,resources=macbuilds,verbs=get;list;watch;delete`
- [ ] Implement Reconcile Logic:
  - [ ] Fetch the `MacBuild` object.
  - [ ] Check if `Status.Phase is Succeeded` or Failed.
  - [ ] Check if `Spec.TtlSecondsAfterFinished` is set.
  - [ ] Check if `Status.CompletionTime` is set.
  - [ ] Decision: `time.Now().After(CompletionTime.Add(TTL))`.
  - [ ] Action: `r.Delete(ctx, &macBuild)`.
  - [ ] Requeue: If not expired, use `ctrl.Result{RequeueAfter: remainingTime}`.

- [ ] Registration:
  - [ ] Register this new controller in main.go.

## 📦 Phase 3: Build & Deployment Configuration

Tasks:

- [ ] Multi-mode Startup (main.go):
  - [ ] Modify `main.go` to add command-line flags to control which controller starts.
  - [ ] `--enable-agent`: Start Agent Reconciler (default: false).
  - [ ] `--enable-gc`: Start GC Reconciler (default: true).
  - Goal: Single binary. Runs in Agent mode on Mac, and GC mode in
