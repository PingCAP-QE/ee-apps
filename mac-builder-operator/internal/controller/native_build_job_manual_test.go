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
	"os"
	"testing"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

// ManualTestNativeBuildJob runs a manual test for the nativeBuildJob.
// This test is intended to be run manually and requires:
// 1. A valid Git repository for the source.
// 2. Proper environment setup (e.g., Go, Git, etc.).
// 3. Network access to clone the repository.
//
// Usage:
//
//	go test -v -run TestManualNativeBuildJob ./internal/controller
func TestManualNativeBuildJob(t *testing.T) {
	// Initialize logger
	logf.SetLogger(zap.New(zap.UseDevMode(true)))

	// Create a sample MacBuild object similar to sample.yaml
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "pd-build-test-manual",
			Namespace: "default",
		},
		Spec: buildv1alpha1.MacBuildSpec{
			Source: buildv1alpha1.Source{
				GitRepository: "https://github.com/tikv/pd.git",
				GitRef:        "master",
			},
			Build: buildv1alpha1.Build{
				Component: "pd",
				Version:   "v8.5.5",
				Arch:      "amd64",
				Profile:   "release",
			},
			Artifacts: buildv1alpha1.Artifacts{
				Push:     false, // Set to true if you want to test pushing artifacts
				Registry: "hub.pingcap.net",
			},
			TTLSecondsAfterFinished: 3600,
		},
	}

	// Initialize a minimal MacBuildReconciler
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("Failed to add scheme: %v", err)
	}

	reconciler := &MacBuildReconciler{
		Client:   &fakeClient{}, // Use a fake client for manual testing
		Scheme:   scheme,
		WorkerID: "manual-test-worker",
	}

	// Create a context
	ctx := context.Background()
	logger := logf.FromContext(ctx)
	logger.Info("Starting manual test for nativeBuildJob")

	// Create a new nativeBuildJob
	job := newNativeBuildJob(reconciler, ctx, *macBuild)
	logger.Info("Created nativeBuildJob", "job", job)

	// Run the job
	result, err := job.Run()
	if err != nil {
		logger.Error(err, "Failed to run nativeBuildJob")
		t.Fatalf("Failed to run nativeBuildJob: %v", err)
	}

	// Print the result
	logger.Info("Build completed successfully",
		"commitHash", result.CommitHash,
		"pushedArtifactsYaml", result.PushedArtifactsYaml,
	)
	fmt.Printf("Manual test completed successfully!\nCommit Hash: %s\n", result.CommitHash)
}

// fakeClient is a minimal implementation of client.Client for manual testing.
type fakeClient struct {
	client.Client
}

func (f *fakeClient) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	return nil
}

func (f *fakeClient) Update(ctx context.Context, obj client.Object, opts ...client.UpdateOption) error {
	return nil
}

func (f *fakeClient) Status() client.StatusWriter {
	return &fakeStatusWriter{}
}

type fakeStatusWriter struct {
	client.StatusWriter
}

func (f *fakeStatusWriter) Update(ctx context.Context, obj client.Object, opts ...client.UpdateOption) error {
	return nil
}

// ExampleTestNativeBuildJobWithLocalRepo demonstrates how to test with a local repository.
// This is useful for testing without network access.
func ExampleTestNativeBuildJobWithLocalRepo() {
	// Initialize logger
	logf.SetLogger(zap.New(zap.UseDevMode(true)))

	// Create a sample MacBuild object with a local repository path
	localRepoPath, err := os.Getwd() // Replace with your local repository path
	if err != nil {
		fmt.Printf("Failed to get working directory: %v\n", err)
		return
	}

	// Use a local repository for testing
	macBuild := &buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "local-build-test",
			Namespace: "default",
		},
		Spec: buildv1alpha1.MacBuildSpec{
			Source: buildv1alpha1.Source{
				GitRepository: fmt.Sprintf("file://%s", localRepoPath),
				GitRef:        "main", // Replace with your branch
			},
			Build: buildv1alpha1.Build{
				Component: "example-component", // Replace with your component
				Version:   "v0.1.0",
				Arch:      "amd64",
				Profile:   "release",
			},
			Artifacts: buildv1alpha1.Artifacts{
				Push:     false,
				Registry: "hub.pingcap.net",
			},
			TTLSecondsAfterFinished: 3600,
		},
	}

	// Initialize a minimal MacBuildReconciler
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		fmt.Printf("Failed to add scheme: %v\n", err)
		return
	}

	reconciler := &MacBuildReconciler{
		Client:   &fakeClient{},
		Scheme:   scheme,
		WorkerID: "manual-test-worker",
	}

	// Create a context
	ctx := context.Background()
	logger := logf.FromContext(ctx)
	logger.Info("Starting manual test for nativeBuildJob with local repository")

	// Create a new nativeBuildJob
	job := newNativeBuildJob(reconciler, ctx, *macBuild)
	logger.Info("Created nativeBuildJob", "job", job)

	// Run the job
	result, err := job.Run()
	if err != nil {
		logger.Error(err, "Failed to run nativeBuildJob")
		fmt.Printf("Failed to run nativeBuildJob: %v\n", err)
		return
	}

	// Print the result
	logger.Info("Build completed successfully",
		"commitHash", result.CommitHash,
		"pushedArtifactsYaml", result.PushedArtifactsYaml,
	)
	fmt.Printf("Manual test with local repository completed successfully!\nCommit Hash: %s\n", result.CommitHash)
}
