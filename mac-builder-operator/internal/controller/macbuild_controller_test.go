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

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

var _ = Describe("MacBuild Controller", func() {
	Context("When reconciling a resource", func() {
		const resourceName = "test-resource"

		ctx := context.Background()

		typeNamespacedName := types.NamespacedName{
			Name:      resourceName,
			Namespace: "default", // TODO(user):Modify as needed
		}
		macbuild := &buildv1alpha1.MacBuild{}

		BeforeEach(func() {
			By("creating the custom resource for the Kind MacBuild")
			err := k8sClient.Get(ctx, typeNamespacedName, macbuild)
			if err != nil && errors.IsNotFound(err) {
				resource := &buildv1alpha1.MacBuild{
					ObjectMeta: metav1.ObjectMeta{
						Name:      resourceName,
						Namespace: "default",
					},
					Spec: buildv1alpha1.MacBuildSpec{
						Source: buildv1alpha1.SourceSpec{
							GitRepository: "https://github.com/pingcap/tidb.git",
							GitRef:        "main",
						},
						Build: buildv1alpha1.BuildSpec{
							Component: "tidb",
							Version:   "nightly",
						},
						Artifacts: buildv1alpha1.ArtifactsSpec{},
					},
				}
				Expect(k8sClient.Create(ctx, resource)).To(Succeed())
			}
		})

		AfterEach(func() {
			// TODO(user): Cleanup logic after each test, like removing the resource instance.
			resource := &buildv1alpha1.MacBuild{}
			err := k8sClient.Get(ctx, typeNamespacedName, resource)
			Expect(err).NotTo(HaveOccurred())

			By("Cleanup the specific resource instance MacBuild")
			Expect(k8sClient.Delete(ctx, resource)).To(Succeed())
		})
		It("should successfully reconcile the resource", func() {
			By("Reconciling the created resource")
			controllerReconciler := &MacBuildReconciler{
				Client: k8sClient,
				Scheme: k8sClient.Scheme(),
			}

			_, err := controllerReconciler.Reconcile(ctx, reconcile.Request{
				NamespacedName: typeNamespacedName,
			})
			Expect(err).NotTo(HaveOccurred())
			// TODO(user): Add more specific assertions depending on your controller's reconciliation logic.
			// Example: If you expect a certain status condition after reconciliation, verify it here.
		})

		It("should only claim builds that match the worker arch and record phase progression", func() {
			resourceName := "arch-aware-resource"
			key := types.NamespacedName{Name: resourceName, Namespace: "default"}
			resource := &buildv1alpha1.MacBuild{
				ObjectMeta: metav1.ObjectMeta{
					Name:      resourceName,
					Namespace: "default",
				},
				Spec: buildv1alpha1.MacBuildSpec{
					Source: buildv1alpha1.SourceSpec{
						GitRepository: "https://github.com/pingcap/tidb.git",
						GitRef:        "main",
					},
					Build: buildv1alpha1.BuildSpec{
						Component: "tidb",
						Version:   "nightly",
						Arch:      buildv1alpha1.BuildArchAMD64,
					},
					Artifacts: buildv1alpha1.ArtifactsSpec{
						Push: true,
					},
				},
			}
			Expect(k8sClient.Create(ctx, resource)).To(Succeed())
			DeferCleanup(func() {
				_ = k8sClient.Delete(ctx, resource)
			})

			foreignReconciler := &MacBuildReconciler{
				Client:     k8sClient,
				Scheme:     k8sClient.Scheme(),
				WorkerID:   "worker-arm64",
				WorkerArch: buildv1alpha1.BuildArchARM64,
			}
			_, err := foreignReconciler.Reconcile(ctx, reconcile.Request{NamespacedName: key})
			Expect(err).NotTo(HaveOccurred())
			_, err = foreignReconciler.Reconcile(ctx, reconcile.Request{NamespacedName: key})
			Expect(err).NotTo(HaveOccurred())

			pending := &buildv1alpha1.MacBuild{}
			Expect(k8sClient.Get(ctx, key, pending)).To(Succeed())
			Expect(pending.Status.Phase).To(Equal(buildv1alpha1.PhasePending))
			Expect(pending.Status.WorkerID).To(BeNil())

			matchingReconciler := &MacBuildReconciler{
				Client:     k8sClient,
				Scheme:     k8sClient.Scheme(),
				WorkerID:   "worker-amd64",
				WorkerArch: buildv1alpha1.BuildArchAMD64,
				runBuild: func(ctx context.Context, build buildv1alpha1.MacBuild, reportPhase buildPhaseReporter) (*buildResult, error) {
					Expect(reportPhase(buildv1alpha1.PhaseBuilding, "Running build steps on the worker.")).To(Succeed())
					Expect(reportPhase(buildv1alpha1.PhasePublishing, "Publishing build artifacts.")).To(Succeed())
					return &buildResult{
						CommitHash:          "deadbeef",
						PushedArtifactsYaml: "artifacts:\n- name: tidb\n",
					}, nil
				},
			}
			_, err = matchingReconciler.Reconcile(ctx, reconcile.Request{NamespacedName: key})
			Expect(err).NotTo(HaveOccurred())
			_, err = matchingReconciler.Reconcile(ctx, reconcile.Request{NamespacedName: key})
			Expect(err).NotTo(HaveOccurred())

			updated := &buildv1alpha1.MacBuild{}
			Expect(k8sClient.Get(ctx, key, updated)).To(Succeed())
			Expect(updated.Status.Phase).To(Equal(buildv1alpha1.PhaseSucceeded))
			Expect(updated.Status.WorkerID).NotTo(BeNil())
			Expect(*updated.Status.WorkerID).To(Equal("worker-amd64"))
			Expect(updated.Status.WorkerArch).NotTo(BeNil())
			Expect(*updated.Status.WorkerArch).To(Equal(buildv1alpha1.BuildArchAMD64))
			Expect(updated.Status.PhaseHistory).To(HaveLen(5))
			Expect(updated.Status.PhaseHistory[0].Phase).To(Equal(buildv1alpha1.PhasePending))
			Expect(updated.Status.PhaseHistory[1].Phase).To(Equal(buildv1alpha1.PhasePreparing))
			Expect(updated.Status.PhaseHistory[2].Phase).To(Equal(buildv1alpha1.PhaseBuilding))
			Expect(updated.Status.PhaseHistory[3].Phase).To(Equal(buildv1alpha1.PhasePublishing))
			Expect(updated.Status.PhaseHistory[4].Phase).To(Equal(buildv1alpha1.PhaseSucceeded))
		})
	})
})
