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

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Build phases
const (
	PhasePending   string = "Pending"
	PhaseBuilding  string = "Building"
	PhaseUploading string = "Uploading"
	PhaseSucceeded string = "Succeeded"
	PhaseFailed    string = "Failed"
)

// MacBuildSpec defines the desired state of MacBuild
type MacBuildSpec struct {
	// Source defines the code to be built
	// +kubebuilder:validation:Required
	Source SourceSpec `json:"source"`

	// Build defines the parameters for the build process
	// +kubebuilder:validation:Required
	Build BuildSpec `json:"build"`

	// Artifacts defines where and how to publish the build output
	// +kubebuilder:validation:Required
	Artifacts ArtifactsSpec `json:"artifacts"`

	// Seconds to retain the build resource after it has finished (succeeded or failed).
	// After this time, it will be automatically deleted. If unset, it will be kept indefinitely.
	// +optional
	// +kubebuilder:validation:Minimum=0
	TtlSecondsAfterFinished *int32 `json:"ttlSecondsAfterFinished,omitempty"`
}

// SourceSpec defines the code source
type SourceSpec struct {
	// Git repository URL (e.g., https://github.com/user/repo.git)
	// +kubebuilder:validation:Required
	GitRepository string `json:"gitRepository"`

	// Git ref (branch or tag) (e.g., main, v1.0.0)
	// +kubebuilder:validation:Required
	GitRef string `json:"gitRef"`

	// Optional: A specific git commit SHA to check out. Overrides gitRef if provided.
	// (Maps to Tekton git-sha param)
	// +optional
	GitSha *string `json:"gitSha,omitempty"`

	// Optional: A git refspec to fetch before checkout (e.g., refs/pull/123/head)
	// +optional
	GitRefspec *string `json:"gitRefspec,omitempty"`
}

// BuildSpec defines the build parameters
type BuildSpec struct {
	// Component to build (e.g., tidb, tikv)
	// +kubebuilder:validation:Required
	Component string `json:"component"`

	// Version string to release (e.g., v1.2.3, nightly)
	// +kubebuilder:validation:Required
	Version string `json:"version"`

	// OS to build for.
	// +kubebuilder:default:="darwin"
	OS string `json:"os,omitempty"`

	// Architecture to build for (e.g., amd64, arm64)
	Arch string `json:"arch,omitempty"`

	// Build profile (e.g., release, failpoint)
	// +optional
	// +kubebuilder:default:="release"
	Profile string `json:"profile,omitempty"`
}

// MacBuildOutput defines the build artifact targets
type MacBuildOutput struct {
	// OCI registry path for the raw binary artifact (e.g., .app, .dmg)
	// (e.g., oci://my-registry.com/artifacts/my-app)
	// +kubebuilder:validation:Required
	BinaryRegistry string `json:"binaryRegistry"`

	// Optional: OCI registry path for the container image that includes the binary
	// (e.g., my-registry.com/images/my-app)
	// +optional
	ImageRegistry *string `json:"imageRegistry,omitempty"`
}

// ArtifactsSpec defines the artifact publishing strategy
type ArtifactsSpec struct {
	// Whether to push artifacts to the registry
	// +optional
	// +kubebuilder:default:=false
	Push bool `json:"push,omitempty"`

	// Target registry (e.g., hub.pingcap.net)
	// +optional
	// +kubebuilder:default:="hub.pingcap.net"
	Registry string `json:"registry,omitempty"`
}

// MacBuildStatus defines the observed state of MacBuild
type MacBuildStatus struct {
	// Current phase of the build (Pending, Building, Uploading, Succeeded, Failed)
	// +optional
	// +kubebuilder:validation:Enum=Pending;Building;Uploading;Succeeded;Failed
	Phase string `json:"phase,omitempty"`

	// A detailed message about the current status (e.g., error log)
	// +optional
	Message *string `json:"message,omitempty"`

	// Timestamp when the build process started
	// +optional
	StartTime *metav1.Time `json:"startTime,omitempty"`

	// Timestamp when the build process completed (used for GC)
	// +optional
	CompletionTime *metav1.Time `json:"completionTime,omitempty"`

	// The actual Git Commit SHA that was checked out
	// +optional
	CommitHash *string `json:"commitHash,omitempty"`

	// The ID of the macOS worker that is processing this build
	// +optional
	WorkerID *string `json:"workerID,omitempty"`

	// URLs of the final artifacts after completion
	// +optional
	Outputs *MacBuildResultOutputs `json:"outputs,omitempty"`
}

// MacBuildResultOutputs contains the final URLs of the build artifacts
type MacBuildResultOutputs struct {
	PushedArtifactsYaml *string `json:"pushed_artifacts_yaml,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Status",type="string",JSONPath=".status.phase",description="The current build status"
// +kubebuilder:printcolumn:name="GitRef",type="string",JSONPath=".spec.gitRef",description="Git Ref"
// +kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"

// MacBuild is the Schema for the macbuilds API
type MacBuild struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   MacBuildSpec   `json:"spec,omitempty"`
	Status MacBuildStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// MacBuildList contains a list of MacBuild
type MacBuildList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []MacBuild `json:"items"`
}

func init() {
	SchemeBuilder.Register(&MacBuild{}, &MacBuildList{})
}
