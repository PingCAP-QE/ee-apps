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
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const gcTestNamespace = "mac-builds"

func newGCTestReconciler(t *testing.T, objs ...client.Object) (*MacBuildGCReconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add build scheme: %v", err)
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
	return &MacBuildGCReconciler{Client: c, Scheme: scheme}, c
}

func gcRequest(name string) ctrl.Request {
	return ctrl.Request{NamespacedName: types.NamespacedName{Namespace: gcTestNamespace, Name: name}}
}

func finishedMacBuild(name string, ttl *metav1.Duration, completedAt time.Time) *buildv1alpha1.MacBuild {
	completed := metav1.NewTime(completedAt)
	return &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: gcTestNamespace},
		Spec:       buildv1alpha1.MacBuildSpec{Ttl: ttl},
		Status: buildv1alpha1.MacBuildStatus{
			Phase:          buildv1alpha1.PhaseSucceeded,
			CompletionTime: &completed,
		},
	}
}

func TestGCDeletesExpiredMacBuild(t *testing.T) {
	t.Parallel()

	mb := finishedMacBuild("expired", &metav1.Duration{Duration: time.Hour}, time.Now().Add(-2*time.Hour))
	r, c := newGCTestReconciler(t, mb)

	if _, err := r.Reconcile(context.Background(), gcRequest(mb.Name)); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}

	var out buildv1alpha1.MacBuild
	err := c.Get(context.Background(), types.NamespacedName{Namespace: gcTestNamespace, Name: mb.Name}, &out)
	if !apierrors.IsNotFound(err) {
		t.Fatalf("expected expired MacBuild to be deleted, got err=%v", err)
	}
}

func TestGCKeepsUnexpiredMacBuildAndRequeues(t *testing.T) {
	t.Parallel()

	mb := finishedMacBuild("fresh", &metav1.Duration{Duration: time.Hour}, time.Now().Add(-30*time.Minute))
	r, c := newGCTestReconciler(t, mb)

	result, err := r.Reconcile(context.Background(), gcRequest(mb.Name))
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result.RequeueAfter <= 0 {
		t.Fatalf("expected a requeue for an unexpired MacBuild, got %s", result.RequeueAfter)
	}

	var out buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: gcTestNamespace, Name: mb.Name}, &out); err != nil {
		t.Fatalf("expected unexpired MacBuild to remain, got err=%v", err)
	}
}

func TestGCSkipsWhenTTLUnset(t *testing.T) {
	t.Parallel()

	mb := finishedMacBuild("no-ttl", nil, time.Now().Add(-48*time.Hour))
	r, c := newGCTestReconciler(t, mb)

	result, err := r.Reconcile(context.Background(), gcRequest(mb.Name))
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Fatalf("expected no requeue when ttl is unset, got %s", result.RequeueAfter)
	}

	var out buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: gcTestNamespace, Name: mb.Name}, &out); err != nil {
		t.Fatalf("expected MacBuild without ttl to remain, got err=%v", err)
	}
}

func TestGCSkipsUnfinishedMacBuild(t *testing.T) {
	t.Parallel()

	mb := finishedMacBuild("running", &metav1.Duration{Duration: time.Hour}, time.Now().Add(-2*time.Hour))
	mb.Status.Phase = buildv1alpha1.PhaseBuilding
	r, c := newGCTestReconciler(t, mb)

	result, err := r.Reconcile(context.Background(), gcRequest(mb.Name))
	if err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Fatalf("expected no requeue for an unfinished MacBuild, got %s", result.RequeueAfter)
	}

	var out buildv1alpha1.MacBuild
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: gcTestNamespace, Name: mb.Name}, &out); err != nil {
		t.Fatalf("expected unfinished MacBuild to remain, got err=%v", err)
	}
}
