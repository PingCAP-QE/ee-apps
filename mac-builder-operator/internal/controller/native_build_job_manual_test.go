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
	"testing"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
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
			Source: buildv1alpha1.SourceSpec{
				GitRepository: "https://github.com/tikv/pd.git",
				GitRef:        "master",
			},
			Build: buildv1alpha1.BuildSpec{
				Component: "pd",
				Version:   "v8.5.5",
				Arch:      "amd64",
				Profile:   "release",
			},
			Artifacts: buildv1alpha1.ArtifactsSpec{
				Push:     true, // Set to true if you want to test pushing artifacts
				Registry: "hub.pingcap.net/devbuild",
			},
			TtlSecondsAfterFinished: ptr.To[int32](3600),
		},
	}

	// Initialize a minimal MacBuildReconciler
	scheme := runtime.NewScheme()
	if err := buildv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("Failed to add scheme: %v", err)
	}

	// Create a context
	ctx := context.Background()
	logger := logf.FromContext(ctx)
	logger.Info("Starting manual test for nativeBuildJob")

	// Create a new nativeBuildJob
	job := newNativeBuildJob(ctx, *macBuild)
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
