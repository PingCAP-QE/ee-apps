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
	"strconv"
	"strings"
	"time"

	"github.com/go-logr/logr"
	tektonv1beta1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"knative.dev/pkg/apis"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const (
	// MacBuild identifiers referenced by the CustomRun customRef.
	macBuildAPIVersion = "tibuild.pingcap.net/v1alpha1"
	macBuildKind       = "MacBuild"

	// Labels linking a MacBuild back to its CustomRun.
	labelCustomRunName      = "tibuild.pingcap.net/customrun-name"
	labelCustomRunNamespace = "tibuild.pingcap.net/customrun-namespace"

	defaultCustomRunPollInterval = 30 * time.Second
)

// MacBuildCustomRunReconciler reconciles Tekton CustomRun objects that reference
// the MacBuild kind: it creates the MacBuild, watches it, and mirrors its status
// back into the CustomRun conditions/results.
type MacBuildCustomRunReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// MacBuildNamespace is the namespace where MacBuild objects are created.
	MacBuildNamespace string
	// PollInterval is how often running CustomRuns are re-checked.
	PollInterval time.Duration

	now func() time.Time
}

// +kubebuilder:rbac:groups=tekton.dev,resources=customruns,verbs=get;list;watch
// +kubebuilder:rbac:groups=tekton.dev,resources=customruns/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=tibuild.pingcap.net,resources=macbuilds/status,verbs=get;list;watch

// Reconcile implements the MacBuild-backed CustomRun contract.
func (r *MacBuildCustomRunReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := logf.FromContext(ctx)

	var cr tektonv1beta1.CustomRun
	if err := r.Get(ctx, req.NamespacedName, &cr); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	if !isMacBuildCustomRun(&cr) {
		// Not ours: ignore so other Custom Task controllers can handle it.
		return ctrl.Result{}, nil
	}

	namespace := r.macBuildNamespace()
	name := cr.GetName()

	var macBuild buildv1alpha1.MacBuild
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &macBuild)
	switch {
	case apierrors.IsNotFound(err):
		mb, buildErr := buildMacBuildFromCustomRun(&cr, namespace, name)
		if buildErr != nil {
			return ctrl.Result{}, buildErr
		}
		if createErr := r.Create(ctx, mb); createErr != nil && !apierrors.IsAlreadyExists(createErr) {
			return ctrl.Result{}, fmt.Errorf("failed to create MacBuild %s/%s: %w", namespace, name, createErr)
		}
		logger.Info("Created MacBuild for CustomRun", "macBuild", name, "customRun", cr.GetName())
		return ctrl.Result{RequeueAfter: r.pollInterval()}, nil
	case err != nil:
		return ctrl.Result{}, err
	}

	return r.syncCustomRunStatus(ctx, logger, &cr, &macBuild)
}

func (r *MacBuildCustomRunReconciler) syncCustomRunStatus(
	ctx context.Context,
	logger logr.Logger,
	cr *tektonv1beta1.CustomRun,
	macBuild *buildv1alpha1.MacBuild,
) (ctrl.Result, error) {
	conditionStatus, reason, message := mapPhaseToCondition(macBuild.Status)

	if err := r.updateCustomRunStatus(ctx, cr, conditionStatus, reason, message, macBuild, r.currentTime()); err != nil {
		return ctrl.Result{}, err
	}

	logger.Info("Synced CustomRun status", "customRun", cr.GetName(), "macBuildPhase", macBuild.Status.Phase, "condition", conditionStatus)

	if conditionStatus == corev1.ConditionUnknown {
		return ctrl.Result{RequeueAfter: r.pollInterval()}, nil
	}
	return ctrl.Result{}, nil
}

const (
	reasonSucceeded = "Succeeded"
	reasonFailed    = "Failed"
	reasonPending   = "Pending"
	reasonRunning   = "Running"
)

// mapPhaseToCondition maps a MacBuild status to the CustomRun Succeeded condition.
func mapPhaseToCondition(status buildv1alpha1.MacBuildStatus) (conditionStatus corev1.ConditionStatus, reason, message string) {
	switch status.Phase {
	case buildv1alpha1.PhaseSucceeded:
		return corev1.ConditionTrue, reasonSucceeded, "MacBuild completed successfully."
	case buildv1alpha1.PhaseFailed:
		msg := "MacBuild failed."
		if status.Message != nil && *status.Message != "" {
			msg = *status.Message
		} else if status.PhaseMessage != nil && *status.PhaseMessage != "" {
			msg = *status.PhaseMessage
		}
		return corev1.ConditionFalse, reasonFailed, msg
	case "":
		return corev1.ConditionUnknown, reasonPending, "Waiting for MacBuild to start."
	default:
		msg := fmt.Sprintf("MacBuild phase: %s", status.Phase)
		if status.PhaseMessage != nil && *status.PhaseMessage != "" {
			msg = *status.PhaseMessage
		}
		return corev1.ConditionUnknown, reasonRunning, msg
	}
}

func (r *MacBuildCustomRunReconciler) updateCustomRunStatus(
	ctx context.Context,
	cr *tektonv1beta1.CustomRun,
	conditionStatus corev1.ConditionStatus,
	reason, message string,
	macBuild *buildv1alpha1.MacBuild,
	now time.Time,
) error {
	var latest tektonv1beta1.CustomRun
	if err := r.Get(ctx, client.ObjectKeyFromObject(cr), &latest); err != nil {
		return err
	}

	// Skip no-op writes: re-writing an identical status would trigger another
	// reconcile event and spin a hot loop.
	if existing := latest.Status.GetCondition(apis.ConditionSucceeded); existing != nil &&
		existing.Status == conditionStatus && existing.Reason == reason && existing.Message == message {
		return nil
	}

	if latest.Status.StartTime == nil {
		start := macBuild.Status.StartTime
		if start == nil {
			start = &metav1.Time{Time: now}
		}
		latest.Status.StartTime = start
	}

	latest.Status.SetCondition(&apis.Condition{
		Type:               apis.ConditionSucceeded,
		Status:             conditionStatus,
		Reason:             reason,
		Message:            message,
		LastTransitionTime: apis.VolatileTime{Inner: metav1.NewTime(now)},
	})

	if conditionStatus != corev1.ConditionUnknown {
		completion := now
		if macBuild.Status.CompletionTime != nil {
			completion = macBuild.Status.CompletionTime.Time
		}
		latest.Status.CompletionTime = &metav1.Time{Time: completion}
	}

	if conditionStatus == corev1.ConditionTrue && macBuild.Status.Outputs != nil && macBuild.Status.Outputs.PushedArtifactsYaml != nil {
		latest.Status.Results = []tektonv1beta1.CustomRunResult{
			{Name: "pushed", Value: *macBuild.Status.Outputs.PushedArtifactsYaml},
		}
	}

	return r.Status().Update(ctx, &latest)
}

// SetupWithManager sets up the controller with the Manager.
func (r *MacBuildCustomRunReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&tektonv1beta1.CustomRun{}).
		Named("macbuild-customrun").
		Complete(r)
}

func (r *MacBuildCustomRunReconciler) macBuildNamespace() string {
	if strings.TrimSpace(r.MacBuildNamespace) != "" {
		return r.MacBuildNamespace
	}
	return "default"
}

func (r *MacBuildCustomRunReconciler) pollInterval() time.Duration {
	if r.PollInterval > 0 {
		return r.PollInterval
	}
	return defaultCustomRunPollInterval
}

func (r *MacBuildCustomRunReconciler) currentTime() time.Time {
	if r.now != nil {
		return r.now()
	}
	return time.Now()
}

func isMacBuildCustomRun(cr *tektonv1beta1.CustomRun) bool {
	ref := cr.Spec.CustomRef
	if ref == nil {
		return false
	}
	return ref.APIVersion == macBuildAPIVersion && string(ref.Kind) == macBuildKind
}

// customRunParams flattens spec.params into a name -> string map.
func customRunParams(cr *tektonv1beta1.CustomRun) map[string]string {
	out := make(map[string]string, len(cr.Spec.Params))
	for _, p := range cr.Spec.Params {
		out[p.Name] = p.Value.StringVal
	}
	return out
}

// buildMacBuildFromCustomRun translates a CustomRun into a MacBuild using the
// frozen contract (see the track design note).
func buildMacBuildFromCustomRun(cr *tektonv1beta1.CustomRun, namespace, name string) (*buildv1alpha1.MacBuild, error) {
	params := customRunParams(cr)

	gitURL := params["git-url"]
	component := params["component"]
	if gitURL == "" {
		return nil, fmt.Errorf("customrun %s/%s: missing required param %q", cr.GetNamespace(), cr.GetName(), "git-url")
	}
	if component == "" {
		return nil, fmt.Errorf("customrun %s/%s: missing required param %q", cr.GetNamespace(), cr.GetName(), "component")
	}

	gitRef := firstNonEmpty(params["git-ref"], params["git-revision"])
	gitSha := params["git-revision"]

	spec := buildv1alpha1.MacBuildSpec{
		Source: buildv1alpha1.SourceSpec{
			GitRepository: gitURL,
			GitRef:        gitRef,
		},
		Build: buildv1alpha1.BuildSpec{
			Component: component,
			Version:   params["version"],
			OS:        firstNonEmpty(params["os"], "darwin"),
			Arch:      buildv1alpha1.NormalizeBuildArch(params["arch"]),
			Profile:   firstNonEmpty(params["profile"], "release"),
		},
		Artifacts: buildv1alpha1.ArtifactsSpec{
			Push:     parseBool(params["push"]),
			Registry: params["registry"],
		},
	}
	if gitSha != "" {
		spec.Source.GitSha = &gitSha
	}
	// Only pass a refspec for pull-request refs (`refs/pull/N/head`). Branch/tag
	// builds check out `gitSha` directly; a `+refs/heads/*` refspec would target
	// the currently checked-out branch and make `git fetch` refuse.
	if refspec := params["git-refspec"]; strings.Contains(refspec, "refs/pull/") {
		spec.Source.GitRefspec = &refspec
	}
	if ttl := parseInt32(params["ttl-seconds-after-finished"]); ttl != nil {
		spec.TtlSecondsAfterFinished = ttl
	}

	return &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels: map[string]string{
				labelCustomRunName:      cr.GetName(),
				labelCustomRunNamespace: cr.GetNamespace(),
			},
		},
		Spec: spec,
	}, nil
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

func parseBool(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "true", "1", "yes":
		return true
	default:
		return false
	}
}

func parseInt32(value string) *int32 {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	n, err := strconv.ParseInt(strings.TrimSpace(value), 10, 32)
	if err != nil {
		return nil
	}
	v := int32(n)
	return &v
}
