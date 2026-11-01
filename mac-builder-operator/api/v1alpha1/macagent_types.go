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
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// MacAgent phases describe the heartbeat and maintenance state of a machine.
const (
	MacAgentPhaseOnline   string = "Online"
	MacAgentPhaseCordoned string = "Cordoned"
	MacAgentPhaseDraining string = "Draining"
	MacAgentPhaseDrained  string = "Drained"
	MacAgentPhaseOffline  string = "Offline"
	MacAgentPhaseUnknown  string = "Unknown"
)

// MacAgent condition types.
const (
	MacAgentConditionReady        string = "Ready"
	MacAgentConditionSchedulable  string = "Schedulable"
	MacAgentConditionDiskPressure string = "DiskPressure"
	MacAgentConditionDraining     string = "Draining"
)

// MacAgent condition reasons.
const (
	MacAgentReasonNoHeartbeat    string = "NoHeartbeat"
	MacAgentReasonHeartbeatOK    string = "HeartbeatOK"
	MacAgentReasonHeartbeatLost  string = "HeartbeatLost"
	MacAgentReasonNotReady       string = "NotReady"
	MacAgentReasonSchedulable    string = "Schedulable"
	MacAgentReasonCapacityFull   string = "CapacityFull"
	MacAgentReasonDiskPressure   string = "DiskPressure"
	MacAgentReasonSufficientDisk string = "SufficientDisk"
	MacAgentReasonCordoned       string = "Cordoned"
	MacAgentReasonDraining       string = "Draining"
	MacAgentReasonNotDraining    string = "NotDraining"
)

// MacAgent address types, mirroring the Node address vocabulary.
const (
	MacAgentAddressTypeInternalIP string = "InternalIP"
	MacAgentAddressTypeHostname   string = "Hostname"
)

// MacAgent machine identity sources.
const (
	MacAgentMachineIDSourcePlatformUUID string = "platform-uuid"
	MacAgentMachineIDSourceSerial       string = "serial"
	MacAgentMachineIDSourceHostname     string = "hostname"
)

// MacAgent defaults and validation bounds.
const (
	DefaultMacAgentCapacity           int32 = 1
	MinMacAgentCapacity               int32 = 1
	MaxMacAgentCapacity               int32 = 8
	DefaultMacAgentMinFreeDiskBytes   int64 = 20 * 1024 * 1024 * 1024
	MinMacAgentMinFreeDiskBytes       int64 = 1 * 1024 * 1024 * 1024
	DefaultMacAgentOfflineGracePeriod       = 10 * time.Minute
	MinMacAgentOfflineGracePeriod           = time.Minute
	MaxMacAgentOfflineGracePeriod           = time.Hour
)

// MacAgentSpec defines the desired scheduling state of a macOS worker.
type MacAgentSpec struct {
	// Unschedulable stops new builds from being assigned to this agent while
	// letting active builds continue. It is the cordon switch.
	// +optional
	Unschedulable bool `json:"unschedulable,omitempty"`

	// Drain stops new assignments and waits for active builds to finish before
	// the machine is upgraded or taken offline. It does not kill active builds.
	// +optional
	Drain bool `json:"drain,omitempty"`

	// Capacity is the maximum number of concurrent builds this agent accepts.
	// +optional
	// +kubebuilder:default:=1
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=8
	Capacity int32 `json:"capacity,omitempty"`

	// MinFreeDiskBytes is the free-disk threshold below which the scheduler no
	// longer assigns new builds to this agent.
	// +optional
	// +kubebuilder:default:=21474836480
	// +kubebuilder:validation:Minimum=1073741824
	MinFreeDiskBytes int64 `json:"minFreeDiskBytes,omitempty"`

	// OfflineGracePeriod is how long a running build assigned to this agent is
	// kept alive after the agent stops heartbeating. When it expires the build
	// is marked Failed(AgentLost).
	// +optional
	// +kubebuilder:validation:XValidation:rule="duration(self) >= duration('1m') && duration(self) <= duration('1h')",message="offlineGracePeriod must be between 1m and 1h"
	OfflineGracePeriod *metav1.Duration `json:"offlineGracePeriod,omitempty"`
}

// CapacityOrDefault returns the configured capacity or the API default.
func (s MacAgentSpec) CapacityOrDefault() int32 {
	if s.Capacity < MinMacAgentCapacity {
		return DefaultMacAgentCapacity
	}
	return s.Capacity
}

// MinFreeDiskBytesOrDefault returns the configured disk threshold or the API default.
func (s MacAgentSpec) MinFreeDiskBytesOrDefault() int64 {
	if s.MinFreeDiskBytes < MinMacAgentMinFreeDiskBytes {
		return DefaultMacAgentMinFreeDiskBytes
	}
	return s.MinFreeDiskBytes
}

// OfflineGracePeriodOrDefault returns the configured grace period or the API default.
func (s MacAgentSpec) OfflineGracePeriodOrDefault() time.Duration {
	if s.OfflineGracePeriod == nil || s.OfflineGracePeriod.Duration < MinMacAgentOfflineGracePeriod {
		return DefaultMacAgentOfflineGracePeriod
	}
	return s.OfflineGracePeriod.Duration
}

// MacAgentStatus defines the observed state of a macOS worker.
type MacAgentStatus struct {
	// Phase summarizes heartbeat and maintenance state.
	// +optional
	// +kubebuilder:validation:Enum=Online;Cordoned;Draining;Drained;Offline;Unknown
	Phase string `json:"phase,omitempty"`

	// Summary is a one-line human-readable machine summary for kubectl describe.
	// +optional
	Summary string `json:"summary,omitempty"`

	// Hostname is the machine's current hostname.
	// +optional
	Hostname string `json:"hostname,omitempty"`

	// IP is the primary IPv4 address of the machine.
	// +optional
	IP string `json:"ip,omitempty"`

	// Addresses lists the machine's addresses (InternalIP, Hostname).
	// +optional
	// +listType=atomic
	Addresses []MacAgentAddress `json:"addresses,omitempty"`

	// MachineID records which stable identifier was used for this agent.
	// +optional
	MachineID MacAgentMachineID `json:"machineID,omitempty"`

	// Arch is the CPU architecture this agent can build for.
	// +optional
	// +kubebuilder:validation:Enum=amd64;arm64
	Arch string `json:"arch,omitempty"`

	// OS describes the macOS version.
	// +optional
	OS MacAgentOS `json:"os,omitempty"`

	// Hardware describes the machine's static specifications.
	// +optional
	Hardware MacAgentHardware `json:"hardware,omitempty"`

	// Runtime describes the machine's current resource usage.
	// +optional
	Runtime MacAgentRuntime `json:"runtime,omitempty"`

	// AgentVersion is the version of the agent binary reporting this status.
	// +optional
	AgentVersion string `json:"agentVersion,omitempty"`

	// InstanceID identifies the current agent process. It changes on restart and
	// helps detect duplicate agents using the same machine identity.
	// +optional
	InstanceID string `json:"instanceID,omitempty"`

	// ConnectedSince is when the current agent process first registered.
	// +optional
	ConnectedSince *metav1.Time `json:"connectedSince,omitempty"`

	// OnlineDuration is the controller-rendered online duration for describe output.
	// +optional
	OnlineDuration string `json:"onlineDuration,omitempty"`

	// LastHeartbeatTime is the last heartbeat observed from the agent.
	// +optional
	LastHeartbeatTime *metav1.Time `json:"lastHeartbeatTime,omitempty"`

	// LastAssignedAt is when the scheduler last assigned a build to this agent.
	// +optional
	LastAssignedAt *metav1.Time `json:"lastAssignedAt,omitempty"`

	// ActiveBuilds is the number of non-terminal builds assigned to this agent.
	// +optional
	ActiveBuilds int32 `json:"activeBuilds,omitempty"`

	// AssignedBuilds lists the non-terminal builds currently assigned to this agent.
	// +optional
	// +listType=atomic
	AssignedBuilds []MacBuildReference `json:"assignedBuilds,omitempty"`

	// Stats contains best-effort lifetime counters.
	// +optional
	Stats MacAgentStats `json:"stats,omitempty"`

	// Conditions represent the latest observations of the agent state.
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// MacAgentAddress is one address advertised by a macOS worker.
type MacAgentAddress struct {
	// Type is the address type.
	// +kubebuilder:validation:Enum=InternalIP;Hostname
	Type string `json:"type"`

	// Address is the address value.
	Address string `json:"address"`
}

// MacAgentMachineID records the stable identifier used to derive the object name.
type MacAgentMachineID struct {
	// Source is the identifier source.
	// +optional
	// +kubebuilder:validation:Enum=platform-uuid;serial;hostname
	Source string `json:"source,omitempty"`

	// Value is the raw identifier value.
	// +optional
	Value string `json:"value,omitempty"`
}

// MacAgentOS describes the macOS version.
type MacAgentOS struct {
	// Version is the macOS product version (sw_vers -productVersion).
	// +optional
	Version string `json:"version,omitempty"`

	// Build is the macOS build version (sw_vers -buildVersion).
	// +optional
	Build string `json:"build,omitempty"`

	// Kernel is the kernel version (uname -r).
	// +optional
	Kernel string `json:"kernel,omitempty"`
}

// MacAgentHardware describes the machine's static specifications.
type MacAgentHardware struct {
	// Model is the hardware model identifier (sysctl hw.model).
	// +optional
	Model string `json:"model,omitempty"`

	// CPUModel is the CPU brand string.
	// +optional
	CPUModel string `json:"cpuModel,omitempty"`

	// CPUCores is the number of logical CPUs.
	// +optional
	CPUCores int32 `json:"cpuCores,omitempty"`

	// MemoryBytes is the physical memory size.
	// +optional
	MemoryBytes int64 `json:"memoryBytes,omitempty"`

	// DiskTotalBytes is the total size of the root filesystem.
	// +optional
	DiskTotalBytes int64 `json:"diskTotalBytes,omitempty"`
}

// MacAgentRuntime describes the machine's current resource usage.
type MacAgentRuntime struct {
	// Load1 is the 1-minute load average.
	// +optional
	Load1 float64 `json:"load1,omitempty"`

	// Load5 is the 5-minute load average.
	// +optional
	Load5 float64 `json:"load5,omitempty"`

	// Load15 is the 15-minute load average.
	// +optional
	Load15 float64 `json:"load15,omitempty"`

	// DiskFreeBytes is the free space on the root filesystem.
	// +optional
	DiskFreeBytes int64 `json:"diskFreeBytes,omitempty"`

	// DiskFreePercent is the free-space percentage on the root filesystem.
	// +optional
	DiskFreePercent int32 `json:"diskFreePercent,omitempty"`
}

// MacAgentStats contains best-effort lifetime counters.
type MacAgentStats struct {
	// Completed is the number of successful builds observed on this agent.
	// +optional
	Completed int64 `json:"completed,omitempty"`

	// Failed is the number of failed builds observed on this agent.
	// +optional
	Failed int64 `json:"failed,omitempty"`
}

// MacBuildReference identifies a MacBuild in a namespace.
type MacBuildReference struct {
	// Namespace is the MacBuild namespace.
	Namespace string `json:"namespace"`

	// Name is the MacBuild name.
	Name string `json:"name"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=ma
// +kubebuilder:printcolumn:name="Phase",type="string",JSONPath=".status.phase",description="Heartbeat and maintenance phase"
// +kubebuilder:printcolumn:name="Arch",type="string",JSONPath=".status.arch",description="Build architecture"
// +kubebuilder:printcolumn:name="Host",type="string",JSONPath=".status.hostname",description="Machine hostname"
// +kubebuilder:printcolumn:name="IP",type="string",JSONPath=".status.ip",description="Primary IP"
// +kubebuilder:printcolumn:name="Active",type="integer",JSONPath=".status.activeBuilds",description="Active builds"
// +kubebuilder:printcolumn:name="Capacity",type="integer",JSONPath=".spec.capacity",description="Build capacity"
// +kubebuilder:printcolumn:name="Online",type="date",JSONPath=".status.connectedSince",description="Online duration"
// +kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"

// MacAgent is the Schema for the macagents API.
type MacAgent struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   MacAgentSpec   `json:"spec,omitempty"`
	Status MacAgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// MacAgentList contains a list of MacAgent.
type MacAgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []MacAgent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&MacAgent{}, &MacAgentList{})
}
