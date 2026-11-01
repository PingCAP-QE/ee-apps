/*
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
*/

package controller

import (
	"context"
	"fmt"
	"time"

	"github.com/go-logr/logr"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/state"
)

// MacBuildReconciler reconciles a MacBuild object
type MacBuildReconciler struct {
	client.Client
	Scheme                *runtime.Scheme
	WorkerID              string
	WorkerArch            string
	BuildTimeout          time.Duration
	BuildPollInterval     time.Duration
	ArtifactsScriptSource ArtifactsScriptSourceConfig

	// StateStore persists the in-flight build so a restarted agent can decide
	// whether to rerun, fail, or discard it.
	StateStore *state.Store

	// DiskFreeBytes reports the current free space before a build starts.
	DiskFreeBytes func(context.Context) int64
	// MinFreeDiskBytes is the minimum free space required to start a build.
	MinFreeDiskBytes int64
	// DiskCheckAttempts is the number of disk measurements before failing a
	// build (default 2); DiskCheckRetryInterval is the delay between them.
	DiskCheckAttempts      int
	DiskCheckRetryInterval time.Duration

	// Tracker counts active builds so SIGTERM can wait for them.
	Tracker *BuildTracker

	// APIReader reads directly from the API server (uncached). The reconciler
	// uses it for the reads that decide whether to (re)run a build, so it never
	// acts on a stale phase: a reconcile triggered right after a build completes
	// could otherwise read the previous active phase from the informer cache and
	// restart the whole build. Falls back to Client when nil (e.g. in tests).
	APIReader client.Reader

	now      func() time.Time
	runBuild func(context.Context, buildv1alpha1.MacBuild, buildPhaseReporter) (*buildResult, error)

	restartChecked bool
}

type buildResult struct {
	CommitHash          string
	PushedArtifactsYaml string
}

type buildPhaseReporter func(string, string) error

const (
	defaultBuildTimeout      = 24 * time.Hour
	defaultBuildPollInterval = 5 * time.Minute

	defaultDiskCheckAttempts      = 2
	defaultDiskCheckRetryInterval = 100 * time.Millisecond
)

// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get

// Reconcile is part of the main kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state.
// TODO(user): Modify the Reconcile function to compare the state specified by
// the MacBuild object against the actual cluster state, and then
// perform operations to make the cluster state reflect the state specified by
// the user.
//
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.22.1/pkg/reconcile
func (r *MacBuildReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := logf.FromContext(ctx)
	logger.Info("Reconciling MacBuild...")

	// get macBuild object
	var macBuild buildv1alpha1.MacBuild
	if err := r.reader().Get(ctx, req.NamespacedName, &macBuild); err != nil {
		if apierrors.IsNotFound(err) {
			logger.Info("MacBuild resource not found. Ignoring since object must be deleted.")
			return ctrl.Result{}, nil
		}
		logger.Error(err, "Failed to get MacBuild")
		return ctrl.Result{}, err
	}

	if r.StateStore != nil && !r.restartChecked {
		if err := r.reconcileRestart(ctx); err != nil {
			logger.Error(err, "Failed to reconcile the persisted in-flight build; retrying")
			return ctrl.Result{RequeueAfter: time.Minute}, nil
		}
		r.restartChecked = true
	}

	switch macBuild.Status.Phase {
	case "":
		// The cluster scheduler owns the initial Pending transition.
		logger.V(1).Info("MacBuild has no phase yet; waiting for the scheduler.")
		return ctrl.Result{}, nil
	case buildv1alpha1.PhasePending:
		// The cluster scheduler owns assignment; agents never claim Pending builds.
		logger.V(1).Info("MacBuild is Pending; waiting for the scheduler to assign it.")
		return ctrl.Result{}, nil
	case buildv1alpha1.PhaseAssigned:
		return r.reconcileAssigned(ctx, logger, macBuild)
	case buildv1alpha1.PhasePreparing, buildv1alpha1.PhaseBuilding, buildv1alpha1.PhasePublishing:
		return r.reconcileActive(ctx, logger, macBuild)
	case buildv1alpha1.PhaseSucceeded, buildv1alpha1.PhaseFailed:
		logger.WithValues("phase", macBuild.Status.Phase).Info("Nothing to do.")
		return ctrl.Result{}, nil
	default:
		logger.Info("Unknown phase, ignoring", "Phase", macBuild.Status.Phase)
		return ctrl.Result{}, nil
	}
}

func (r *MacBuildReconciler) reconcileAssigned(
	ctx context.Context,
	logger logr.Logger,
	macBuild buildv1alpha1.MacBuild,
) (ctrl.Result, error) {
	if macBuild.Status.Assignment == nil {
		return r.failBuild(ctx, logger, &macBuild, "assigned build has no assignment")
	}
	if !r.assignedToSelf(macBuild) {
		logger.V(1).Info(
			"Build is assigned to another agent; ignoring.",
			"assignedAgent", macBuild.Status.Assignment.AgentName,
			"workerID", r.WorkerID,
		)
		return ctrl.Result{}, nil
	}
	if !r.matchesBuildArch(macBuild) {
		return r.failBuild(
			ctx,
			logger,
			&macBuild,
			fmt.Sprintf("build arch %q does not match agent arch %q", buildArchFor(macBuild), r.WorkerArch),
		)
	}

	logger.Info("Phase: Assigned. Claiming and setting to Preparing.")
	claimed := false
	updatedBuild, err := r.updateBuildStatus(ctx, client.ObjectKeyFromObject(&macBuild), func(status *buildv1alpha1.MacBuildStatus, now metav1.Time) {
		claimed = false
		if status.Phase != buildv1alpha1.PhaseAssigned {
			return
		}
		if status.Assignment == nil || status.Assignment.AgentName != r.WorkerID {
			return
		}
		status.SetPhase(buildv1alpha1.PhasePreparing, "Build claimed by worker and preparing workspace.", now)
		status.WorkerID = &r.WorkerID
		status.WorkerArch = optionalString(r.WorkerArch)
		status.StartTime = &now
		status.CompletionTime = nil
		status.Message = nil
		if status.Assignment != nil {
			status.Assignment.LeaseDeadline = nil
		}
		claimed = true
	})
	if err != nil {
		logger.Error(err, "Failed to update status to Preparing")
		return ctrl.Result{}, err
	}
	if !claimed {
		logger.Info("Assigned build was already claimed or reassigned. Leaving it.")
		return ctrl.Result{}, nil
	}
	macBuild = *updatedBuild

	return ctrl.Result{Requeue: true}, nil
}

func (r *MacBuildReconciler) reconcileActive(
	ctx context.Context,
	logger logr.Logger,
	macBuild buildv1alpha1.MacBuild,
) (ctrl.Result, error) {
	logger.Info("Phase: Active build phase. Starting or resuming build process.", "phase", macBuild.Status.Phase)

	if macBuild.Status.Assignment == nil {
		return r.failBuild(ctx, logger, &macBuild, "build entered an active phase without an assignment")
	}
	if !r.assignedToSelf(macBuild) {
		logger.V(1).Info(
			"Active build is assigned to another agent; ignoring.",
			"assignedAgent", macBuild.Status.Assignment.AgentName,
			"workerID", r.WorkerID,
		)
		return ctrl.Result{}, nil
	}
	if !r.matchesBuildArch(macBuild) {
		return r.failBuild(
			ctx,
			logger,
			&macBuild,
			fmt.Sprintf("build arch %q does not match agent arch %q", buildArchFor(macBuild), r.WorkerArch),
		)
	}

	if macBuild.Status.StartTime == nil {
		return r.failBuild(ctx, logger, &macBuild, "build entered an active phase without startTime")
	}
	if macBuild.Status.WorkerID == nil || *macBuild.Status.WorkerID == "" {
		return r.failBuild(ctx, logger, &macBuild, "build entered an active phase without workerID")
	}
	if r.hasTimedOut(macBuild.Status.StartTime.Time) {
		return r.failBuild(
			ctx,
			logger,
			&macBuild,
			fmt.Sprintf("build timed out after %s on worker %q", r.buildTimeout(), *macBuild.Status.WorkerID),
		)
	}
	if *macBuild.Status.WorkerID != r.WorkerID {
		return r.failBuild(
			ctx,
			logger,
			&macBuild,
			fmt.Sprintf("build is assigned to %q but status.workerID is %q", r.WorkerID, *macBuild.Status.WorkerID),
		)
	}

	if r.DiskFreeBytes != nil && r.MinFreeDiskBytes > 0 {
		attempts := r.DiskCheckAttempts
		if attempts <= 0 {
			attempts = defaultDiskCheckAttempts
		}
		interval := r.DiskCheckRetryInterval
		if interval <= 0 {
			interval = defaultDiskCheckRetryInterval
		}

		free := buildv1alpha1.MacAgentDiskUnknownBytes
		for attempt := 1; attempt <= attempts; attempt++ {
			free = r.DiskFreeBytes(ctx)
			if free >= 0 {
				break
			}
			if attempt < attempts {
				logger.Info("Disk metrics unavailable; retrying",
					"worker", r.WorkerID, "attempt", attempt, "attempts", attempts)
				select {
				case <-ctx.Done():
					return ctrl.Result{}, ctx.Err()
				case <-time.After(interval):
				}
			}
		}
		if free < 0 {
			logger.Info("Disk metrics unavailable; failing build before start", "worker", r.WorkerID)
			return r.failBuild(
				ctx,
				logger,
				&macBuild,
				"disk metrics unavailable: the worker could not read free disk space",
			)
		}
		if free < r.MinFreeDiskBytes {
			logger.Info("Insufficient disk to start build",
				"worker", r.WorkerID, "freeBytes", free, "requiredBytes", r.MinFreeDiskBytes)
			return r.failBuild(
				ctx,
				logger,
				&macBuild,
				fmt.Sprintf("insufficient disk: %s free, need %s", humanDisk(free), humanDisk(r.MinFreeDiskBytes)),
			)
		}
	}

	result, err := r.runNativeBuild(ctx, macBuild, func(phase string, message string) error {
		updatedBuild, err := r.updateBuildStatus(ctx, client.ObjectKeyFromObject(&macBuild), func(status *buildv1alpha1.MacBuildStatus, now metav1.Time) {
			status.SetPhase(phase, message, now)
			status.WorkerID = &r.WorkerID
			status.WorkerArch = optionalString(r.WorkerArch)
			if status.StartTime == nil {
				status.StartTime = &now
			}
			status.CompletionTime = nil
			status.Message = nil
		})
		if err != nil {
			return err
		}
		macBuild = *updatedBuild
		return nil
	})

	if err != nil {
		logger.Error(err, "Build failed")
		return r.failBuild(ctx, logger, &macBuild, err.Error())
	}

	logger.Info("Build succeeded")
	updatedBuild, err := r.updateBuildStatus(ctx, client.ObjectKeyFromObject(&macBuild), func(status *buildv1alpha1.MacBuildStatus, now metav1.Time) {
		status.SetPhase(buildv1alpha1.PhaseSucceeded, "Build completed successfully.", now)
		status.Message = nil
		status.CompletionTime = &now
		status.WorkerArch = optionalString(r.WorkerArch)
		status.CommitHash = &result.CommitHash
		if result.PushedArtifactsYaml != "" {
			if status.Outputs == nil {
				status.Outputs = &buildv1alpha1.MacBuildResultOutputs{}
			}
			status.Outputs.PushedArtifactsYaml = &result.PushedArtifactsYaml
		}
	})
	if err != nil {
		logger.Error(err, "Failed to update status to Succeeded")
		return ctrl.Result{}, err
	}
	macBuild = *updatedBuild
	return ctrl.Result{}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *MacBuildReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&buildv1alpha1.MacBuild{}).
		Named("macbuild").
		Complete(r)
}

func (r *MacBuildReconciler) runNativeBuild(
	ctx context.Context,
	macBuild buildv1alpha1.MacBuild,
	reportPhase buildPhaseReporter,
) (*buildResult, error) {
	logger := logf.FromContext(ctx)

	if r.Tracker != nil {
		r.Tracker.Begin()
		defer r.Tracker.End()
	}

	if r.StateStore != nil {
		r.persistBuildState(ctx, macBuild, macBuild.Status.Phase)
		defer func() {
			if err := r.StateStore.Clear(); err != nil {
				logger.Error(err, "Failed to clear agent build state")
			}
		}()
	}

	wrappedReportPhase := func(phase string, message string) error {
		err := reportPhase(phase, message)
		r.persistBuildState(ctx, macBuild, phase)
		return err
	}

	if r.runBuild != nil {
		return r.runBuild(ctx, macBuild, wrappedReportPhase)
	}
	job := newNativeBuildJob(ctx, macBuild, r.ArtifactsScriptSource)
	job.credentialsReader = r.secretReader()
	job.reportPhase = wrappedReportPhase
	return job.Run()
}

func (r *MacBuildReconciler) persistBuildState(ctx context.Context, macBuild buildv1alpha1.MacBuild, phase string) {
	if r.StateStore == nil {
		return
	}
	startedAt := r.currentTime()
	if macBuild.Status.StartTime != nil {
		startedAt = macBuild.Status.StartTime.Time
	}
	assignmentID := ""
	if macBuild.Status.Assignment != nil {
		assignmentID = string(macBuild.Status.Assignment.AgentUID)
	}
	if err := r.StateStore.Save(state.State{
		Namespace:    macBuild.Namespace,
		Name:         macBuild.Name,
		AssignmentID: assignmentID,
		Phase:        phase,
		StartedAt:    startedAt,
	}); err != nil {
		logf.FromContext(ctx).Error(err, "Failed to persist agent build state")
	}
}

func (r *MacBuildReconciler) reconcileRestart(ctx context.Context) error {
	logger := logf.FromContext(ctx)

	persisted, err := r.StateStore.Load()
	if err != nil {
		logger.Error(err, "Discarding unreadable persisted agent state")
		return r.StateStore.Clear()
	}
	if persisted == nil {
		return nil
	}

	key := types.NamespacedName{Namespace: persisted.Namespace, Name: persisted.Name}
	var build buildv1alpha1.MacBuild
	if err := r.reader().Get(ctx, key, &build); err != nil {
		if apierrors.IsNotFound(err) {
			return r.StateStore.Clear()
		}
		return err
	}

	if build.Status.Phase == buildv1alpha1.PhaseSucceeded ||
		build.Status.Phase == buildv1alpha1.PhaseFailed ||
		build.Status.Assignment == nil ||
		build.Status.Assignment.AgentName != r.WorkerID {
		return r.StateStore.Clear()
	}

	switch persisted.Phase {
	case buildv1alpha1.PhasePublishing:
		message := fmt.Sprintf(
			"agent restarted while publishing build (last phase %s); refusing to rerun the publish step",
			build.Status.Phase,
		)
		if _, err := r.failBuild(ctx, logger, &build, message); err != nil {
			return err
		}
		return r.StateStore.Clear()
	case buildv1alpha1.PhasePreparing, buildv1alpha1.PhaseBuilding:
		base := build.DeepCopy()
		if build.Status.Assignment != nil {
			build.Status.Assignment.Restarts++
		}
		if err := r.Status().Patch(ctx, &build, client.MergeFrom(base)); err != nil {
			return err
		}
		return r.StateStore.Clear()
	default:
		return r.StateStore.Clear()
	}
}

// secretReader returns the client used to read git credential Secrets. Like
// reader(), it prefers the uncached API reader so secret material is never
// retained in the informer cache.
func (r *MacBuildReconciler) secretReader() client.Reader {
	return r.reader()
}

// reader returns the client used for the reads that decide whether to (re)run a
// build. Reading through the API server (uncached) avoids restarting a build
// from a stale phase still present in the informer cache.
func (r *MacBuildReconciler) reader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

func (r *MacBuildReconciler) currentTime() time.Time {
	if r.now != nil {
		return r.now()
	}
	return time.Now()
}

func (r *MacBuildReconciler) buildTimeout() time.Duration {
	if r.BuildTimeout > 0 {
		return r.BuildTimeout
	}
	return defaultBuildTimeout
}

func (r *MacBuildReconciler) hasTimedOut(startTime time.Time) bool {
	return r.currentTime().After(startTime.Add(r.buildTimeout()))
}

func (r *MacBuildReconciler) failBuild(
	ctx context.Context,
	logger logr.Logger,
	macBuild *buildv1alpha1.MacBuild,
	message string,
) (ctrl.Result, error) {
	logger.Info("Marking build as failed", "message", message)

	updatedBuild, err := r.updateBuildStatus(ctx, client.ObjectKeyFromObject(macBuild), func(status *buildv1alpha1.MacBuildStatus, now metav1.Time) {
		status.SetPhase(buildv1alpha1.PhaseFailed, message, now)
		status.Message = &message
		status.CompletionTime = &now
		if status.WorkerID != nil && *status.WorkerID == r.WorkerID {
			status.WorkerArch = optionalString(r.WorkerArch)
		}
	})
	if err != nil {
		logger.Error(err, "Failed to update status to Failed")
		return ctrl.Result{}, err
	}
	*macBuild = *updatedBuild

	return ctrl.Result{}, nil
}

func (r *MacBuildReconciler) updateBuildStatus(
	ctx context.Context,
	key client.ObjectKey,
	mutate func(*buildv1alpha1.MacBuildStatus, metav1.Time),
) (*buildv1alpha1.MacBuild, error) {
	var updated buildv1alpha1.MacBuild

	if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		if err := r.reader().Get(ctx, key, &updated); err != nil {
			return err
		}

		mutate(&updated.Status, metav1.NewTime(r.currentTime()))
		return r.Status().Update(ctx, &updated)
	}); err != nil {
		return nil, err
	}

	return updated.DeepCopy(), nil
}

func (r *MacBuildReconciler) matchesBuildArch(macBuild buildv1alpha1.MacBuild) bool {
	return buildArchFor(macBuild) == buildv1alpha1.NormalizeBuildArch(r.WorkerArch)
}

func (r *MacBuildReconciler) assignedToSelf(macBuild buildv1alpha1.MacBuild) bool {
	return macBuild.Status.Assignment != nil && macBuild.Status.Assignment.AgentName == r.WorkerID
}

func buildArchFor(macBuild buildv1alpha1.MacBuild) string {
	arch := buildv1alpha1.NormalizeBuildArch(macBuild.Spec.Build.Arch)
	if arch == "" {
		return buildv1alpha1.BuildArchAMD64
	}
	return arch
}

func optionalString(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}
