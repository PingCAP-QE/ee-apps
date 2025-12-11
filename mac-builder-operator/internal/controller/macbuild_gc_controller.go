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
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

// MacBuildGCReconciler reconciles a MacBuild object for garbage collection.
type MacBuildGCReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=build.pingcap.com,resources=macbuilds,verbs=get;list;watch;delete
// +kubebuilder:rbac:groups=build.pingcap.com,resources=macbuilds/status,verbs=get

// Reconcile is part of the main kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state.
// This reconciler specifically handles garbage collection of finished MacBuild resources.
func (r *MacBuildGCReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := logf.FromContext(ctx)
	logger.Info("Reconciling MacBuild for GC...")

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

	// Check if TTL is set. If not, we don't do anything.
	if macBuild.Spec.TtlSecondsAfterFinished == nil {
		logger.V(1).Info("No TtlSecondsAfterFinished set, skipping GC.")
		return ctrl.Result{}, nil
	}

	// Check if the build is in a finished state.
	isFinished := macBuild.Status.Phase == buildv1alpha1.PhaseSucceeded || macBuild.Status.Phase == buildv1alpha1.PhaseFailed
	if !isFinished {
		logger.V(1).Info("MacBuild is not in a finished state, skipping GC.", "phase", macBuild.Status.Phase)
		return ctrl.Result{}, nil
	}

	// Check if CompletionTime is set.
	if macBuild.Status.CompletionTime == nil {
		logger.Info("MacBuild is finished but CompletionTime is not set, skipping GC.")
		return ctrl.Result{}, nil
	}

	// Calculate expiration time.
	ttl := time.Duration(*macBuild.Spec.TtlSecondsAfterFinished) * time.Second
	expirationTime := macBuild.Status.CompletionTime.Time.Add(ttl)
	now := time.Now()

	// Decision: Check if the resource has expired.
	if now.After(expirationTime) {
		// Action: Delete the resource.
		logger.Info("MacBuild has expired, deleting.", "ttl", ttl, "completionTime", macBuild.Status.CompletionTime)
		if err := r.Delete(ctx, &macBuild); err != nil {
			logger.Error(err, "Failed to delete expired MacBuild")
			return ctrl.Result{}, err
		}
		logger.Info("Successfully deleted expired MacBuild.")
		return ctrl.Result{}, nil
	}

	// Requeue: If not expired, requeue for the remaining time.
	requeueAfter := expirationTime.Sub(now)
	logger.Info("MacBuild has not yet expired, requeueing for GC check.", "requeueAfter", requeueAfter)
	return ctrl.Result{RequeueAfter: requeueAfter}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *MacBuildGCReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&buildv1alpha1.MacBuild{}).
		Named("macbuild-gc").
		Complete(r)
}
