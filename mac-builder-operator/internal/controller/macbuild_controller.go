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
	"bytes"
	"context"
	"errors"
	"os/exec"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/go-logr/logr"
)

// MacBuildReconciler reconciles a MacBuild object
type MacBuildReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	WorkerID string
}

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

		newStatus := macBuild.Status.DeepCopy()
		newStatus.Phase = buildv1alpha1.PhasePending
		macBuild.Status = *newStatus

		if err := r.Status().Update(ctx, &macBuild); err != nil {
			logger.Error(err, "Failed to update MacBuild status to Pending")
			return ctrl.Result{}, err
		}

		return ctrl.Result{Requeue: true}, nil
	}

	logger.Info("MacBuild already has status", "Phase", macBuild.Status.Phase)
	switch macBuild.Status.Phase {
	case buildv1alpha1.PhasePending:
		// adopt the build job and update the status to "Building"
		logger.Info("Phase: Pending. Claiming and setting to Building.")
		newStatus := macBuild.Status.DeepCopy()
		newStatus.Phase = buildv1alpha1.PhaseBuilding
		newStatus.WorkerID = &r.WorkerID
		now := metav1.Now()
		newStatus.StartTime = &now
		macBuild.Status = *newStatus

		if err := r.Status().Update(ctx, &macBuild); err != nil {
			logger.Error(err, "Failed to update status to Building")
			return ctrl.Result{}, err
		}

		return ctrl.Result{Requeue: true}, nil
	case buildv1alpha1.PhaseBuilding:
		logger.Info("Phase: Building. Starting build process.")

		if macBuild.Status.WorkerID == nil || *macBuild.Status.WorkerID != r.WorkerID {
			logger.Info("This build is not assigned to me.", "AssignedWorker", macBuild.Status.WorkerID)
			return ctrl.Result{}, nil
		}

		// check the run status.
		// TODO: in production, we need a more complex logic to check the long time goroutine.
		outputs, err := r.runSimulatedBuild(ctx, macBuild)
		newStatus := macBuild.Status.DeepCopy()
		now := metav1.Now()
		newStatus.CompletionTime = &now

		if err != nil {
			logger.Error(err, "Build failed")
			newStatus.Phase = buildv1alpha1.PhaseFailed
			errMsg := err.Error()
			newStatus.Message = &errMsg
		} else {
			logger.Info("Build succeeded")
			newStatus.Phase = buildv1alpha1.PhaseSucceeded
			newStatus.Outputs = outputs
		}

		macBuild.Status = *newStatus
		if errUpdate := r.Status().Update(ctx, &macBuild); errUpdate != nil {
			logger.Error(errUpdate, "Failed to update status to Succeeded/Failed")
			return ctrl.Result{}, errUpdate
		}
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

func (r *MacBuildReconciler) execCommand(logger logr.Logger, cmd *exec.Cmd) error {
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	logger.Info("Executing command", "cmd", cmd.String())

	if err := cmd.Run(); err != nil {
		stderrStr := stderr.String()
		logger.Error(err, "Command execution failed", "stderr", stderrStr)
		return errors.New(stderrStr)
	}

	logger.Info("Command executed successfully", "stdout", stdout.String())
	return nil
}

// runSimulatedBuild simulated the full build.
func (r *MacBuildReconciler) runSimulatedBuild(ctx context.Context, macBuild buildv1alpha1.MacBuild) (*buildv1alpha1.MacBuildResultOutputs, error) {
	logger := logf.FromContext(ctx)

	logger.Info("Simulating: Git Clone", "repo", macBuild.Spec.GitRepository, "ref", macBuild.Spec.GitRef)
	if err := r.execCommand(logger, exec.Command("sleep", "3")); err != nil {
		return nil, errors.New("git clone simulation failed")
	}

	profile := "default-profile" // 默认值
	if macBuild.Spec.Profile != nil {
		profile = *macBuild.Spec.Profile
	}
	logger.Info("Simulating: Build", "profile", profile)
	if err := r.execCommand(logger, exec.Command("sleep", "5")); err != nil {
		return nil, errors.New("build simulation failed")
	}

	logger.Info("Simulating: OCI Upload", "target", macBuild.Spec.Output.BinaryRegistry)
	if err := r.execCommand(logger, exec.Command("sleep", "2")); err != nil {
		return nil, errors.New("OCI upload simulation failed")
	}

	fakeDigest := "sha256:0123456789abcdefa1b2c3d4e5f60001"
	binaryURL := macBuild.Spec.Output.BinaryRegistry + "@" + fakeDigest

	outputs := &buildv1alpha1.MacBuildResultOutputs{
		BinaryOciUrl: &binaryURL,
	}

	if macBuild.Spec.Output.ImageRegistry != nil {
		fakeImageDigest := "sha256:fedcba9876543210f1e2d3c4b5a61110"
		imageURL := *macBuild.Spec.Output.ImageRegistry + "@" + fakeImageDigest
		outputs.ImageOciUrl = &imageURL
	}

	logger.Info("Simulation complete")
	return outputs, nil
}
