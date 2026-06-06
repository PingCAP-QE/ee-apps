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
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
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

	now      func() time.Time
	runBuild func(context.Context, buildv1alpha1.MacBuild, buildPhaseReporter) (*buildResult, error)
}

type buildResult struct {
	CommitHash          string
	PushedArtifactsYaml string
}

type buildPhaseReporter func(string, string) error

const (
	defaultBuildTimeout      = 24 * time.Hour
	defaultBuildPollInterval = 5 * time.Minute
)

// +kubebuilder:rbac:groups=build.tibuild.pingcap.net,resources=macbuilds,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=build.tibuild.pingcap.net,resources=macbuilds/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=build.tibuild.pingcap.net,resources=macbuilds/finalizers,verbs=update

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
	if err := r.Get(ctx, req.NamespacedName, &macBuild); err != nil {
		if apierrors.IsNotFound(err) {
			logger.Info("MacBuild resource not found. Ignoring since object must be deleted.")
			return ctrl.Result{}, nil
		}
		logger.Error(err, "Failed to get MacBuild")
		return ctrl.Result{}, err
	}

	// init the status for the object
	if macBuild.Status.Phase == "" {
		logger.Info("Setting initial status to Pending")

		updatedBuild, err := r.updateBuildStatus(ctx, client.ObjectKeyFromObject(&macBuild), func(status *buildv1alpha1.MacBuildStatus, now metav1.Time) {
			status.SetPhase(buildv1alpha1.PhasePending, "Waiting for a matching macOS worker to claim this build.", now)
		})
		if err != nil {
			logger.Error(err, "Failed to update MacBuild status to Pending")
			return ctrl.Result{}, err
		}
		macBuild = *updatedBuild

		return ctrl.Result{Requeue: true}, nil
	}

	logger.Info("MacBuild already has status", "Phase", macBuild.Status.Phase)
	switch macBuild.Status.Phase {
	case buildv1alpha1.PhasePending:
		if !r.matchesBuildArch(macBuild) {
			logger.Info(
				"Pending build does not match this worker architecture. Leaving it for another worker.",
				"workerArch", r.WorkerArch,
				"buildArch", buildArchFor(macBuild),
			)
			return ctrl.Result{}, nil
		}

		logger.Info("Phase: Pending. Claiming and setting to Preparing.")
		updatedBuild, err := r.updateBuildStatus(ctx, client.ObjectKeyFromObject(&macBuild), func(status *buildv1alpha1.MacBuildStatus, now metav1.Time) {
			status.SetPhase(buildv1alpha1.PhasePreparing, "Build claimed by worker and preparing workspace.", now)
			status.WorkerID = &r.WorkerID
			status.WorkerArch = optionalString(r.WorkerArch)
			status.StartTime = &now
			status.CompletionTime = nil
			status.Message = nil
		})
		if err != nil {
			logger.Error(err, "Failed to update status to Preparing")
			return ctrl.Result{}, err
		}
		macBuild = *updatedBuild

		return ctrl.Result{Requeue: true}, nil
	case buildv1alpha1.PhasePreparing, buildv1alpha1.PhaseBuilding, buildv1alpha1.PhasePublishing:
		logger.Info("Phase: Active build phase. Starting or resuming build process.", "phase", macBuild.Status.Phase)

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
			logger.Info(
				"This build is assigned to another worker. Rechecking later.",
				"assignedWorker", *macBuild.Status.WorkerID,
				"requeueAfter", r.buildPollInterval(),
			)
			return ctrl.Result{RequeueAfter: r.buildPollInterval()}, nil
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
	case buildv1alpha1.PhaseSucceeded, buildv1alpha1.PhaseFailed:
		logger.WithValues("phase", macBuild.Status.Phase).Info("Nothing to do.")
		return ctrl.Result{}, nil
	default:
		logger.Info("Unknown phase, ignoring", "Phase", macBuild.Status.Phase)
		return ctrl.Result{}, nil
	}
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
	if r.runBuild != nil {
		return r.runBuild(ctx, macBuild, reportPhase)
	}
	job := newNativeBuildJob(ctx, macBuild, r.ArtifactsScriptSource)
	job.reportPhase = reportPhase
	return job.Run()
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

func (r *MacBuildReconciler) buildPollInterval() time.Duration {
	if r.BuildPollInterval > 0 {
		return r.BuildPollInterval
	}
	return defaultBuildPollInterval
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
		if err := r.Get(ctx, key, &updated); err != nil {
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
