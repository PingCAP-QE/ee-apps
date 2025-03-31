// Code generated by goa v3.20.0, DO NOT EDIT.
//
// devbuild service
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/internal/service/design -o
// ./service

package devbuild

import (
	"context"
)

// The devbuild service provides operations to manage dev builds.
type Service interface {
	// List devbuild with pagination support
	List(context.Context, *ListPayload) (res []*DevBuild, err error)
	// Create and trigger devbuild
	Create(context.Context, *CreatePayload) (res *DevBuild, err error)
	// Get devbuild
	Get(context.Context, *GetPayload) (res *DevBuild, err error)
	// Update devbuild status
	Update(context.Context, *UpdatePayload) (res *DevBuild, err error)
	// Rerun devbuild
	Rerun(context.Context, *RerunPayload) (res *DevBuild, err error)
	// Ingest a CloudEvent for build events
	IngestEvent(context.Context, *CloudEventIngestEventPayload) (res *CloudEventResponse, err error)
}

// APIName is the name of the API as defined in the design.
const APIName = "tibuild"

// APIVersion is the version of the API as defined in the design.
const APIVersion = "2.0.0"

// ServiceName is the name of the service as defined in the design. This is the
// same value that is set in the endpoint request contexts under the ServiceKey
// key.
const ServiceName = "devbuild"

// MethodNames lists the service method names as defined in the design. These
// are the same values that are set in the endpoint request contexts under the
// MethodKey key.
var MethodNames = [6]string{"list", "create", "get", "update", "rerun", "ingestEvent"}

type BinArtifact struct {
	Component     *string
	OciFile       *OciFile
	Platform      *string
	Sha256OciFile *OciFile
	Sha256URL     *string
	URL           *string
}

type BuildReport struct {
	Binaries       []*BinArtifact
	GitSha         *string
	Images         []*ImageArtifact
	PluginGitSha   *string
	PrintedVersion *string
}

type BuildStatus string

// CloudEventIngestEventPayload is the payload type of the devbuild service
// ingestEvent method.
type CloudEventIngestEventPayload struct {
	// Unique identifier for the event
	ID string
	// Identifies the context in which an event happened
	Source string
	// Describes the type of event related to the originating occurrence
	Type string
	// Content type of the data value
	Datacontenttype *string
	// The version of the CloudEvents specification which the event uses
	Specversion string
	// Identifies the schema that data adheres to
	Dataschema *string
	// Describes the subject of the event in the context of the event producer
	Subject *string
	// Timestamp of when the occurrence happened
	Time string
	// Event payload
	Data any
}

// CloudEventResponse is the result type of the devbuild service ingestEvent
// method.
type CloudEventResponse struct {
	// The ID of the processed CloudEvent
	ID string
	// Processing status
	Status string
	// Additional information about processing result
	Message *string
}

// CreatePayload is the payload type of the devbuild service create method.
type CreatePayload struct {
	// Creator of build
	CreatedBy string
	// Build to create, only spec field is required, others are ignored
	Request *DevBuildSpec
	// Dry run
	Dryrun bool
}

// DevBuild is the result type of the devbuild service create method.
type DevBuild struct {
	ID     int
	Meta   *DevBuildMeta
	Spec   *DevBuildSpec
	Status *DevBuildStatus
}

type DevBuildMeta struct {
	CreatedBy string
	CreatedAt string
	UpdatedAt string
}

type DevBuildSpec struct {
	BuildEnv          *string
	BuilderImg        *string
	Edition           string
	Features          *string
	GitRef            string
	GitSha            *string
	GithubRepo        *string
	IsHotfix          *bool
	IsPushGcr         *bool
	PipelineEngine    *string
	PluginGitRef      *string
	Product           string
	ProductBaseImg    *string
	ProductDockerfile *string
	TargetImg         *string
	Version           string
}

type DevBuildStatus struct {
	BuildReport      *BuildReport
	ErrMsg           *string
	PipelineBuildID  *int
	PipelineStartAt  *string
	PipelineEndAt    *string
	PipelineViewURL  *string
	PipelineViewUrls []string
	Status           BuildStatus
	TektonStatus     *TektonStatus
}

// GetPayload is the payload type of the devbuild service get method.
type GetPayload struct {
	// ID of build
	ID int
	// Whether sync with jenkins
	Sync bool
}

type HTTPError struct {
	Code    int
	Message string
}

type ImageArtifact struct {
	Platform string
	URL      string
}

// ListPayload is the payload type of the devbuild service list method.
type ListPayload struct {
	// The page number of items
	Page int
	// Page size
	PageSize int
	// Filter hotfix
	Hotfix bool
	// What to sort results by
	Sort string
	// The direction of the sort
	Direction string
	// Filter created by
	CreatedBy *string
}

type OciArtifact struct {
	Files []string
	Repo  string
	Tag   string
}

type OciFile struct {
	File string
	Repo string
	Tag  string
}

// RerunPayload is the payload type of the devbuild service rerun method.
type RerunPayload struct {
	// ID of build
	ID int
	// Dry run
	Dryrun bool
}

type TektonPipeline struct {
	Name         string
	Status       BuildStatus
	StartAt      *string
	EndAt        *string
	GitSha       *string
	Images       []*ImageArtifact
	OciArtifacts []*OciArtifact
	Platform     *string
	URL          *string
}

type TektonStatus struct {
	Pipelines []*TektonPipeline
}

// UpdatePayload is the payload type of the devbuild service update method.
type UpdatePayload struct {
	// ID of build
	ID int
	// status update
	Status *DevBuildStatus
	// Dry run
	Dryrun bool
}

// Error returns an error description.
func (e *HTTPError) Error() string {
	return ""
}

// ErrorName returns "HTTPError".
//
// Deprecated: Use GoaErrorName - https://github.com/goadesign/goa/issues/3105
func (e *HTTPError) ErrorName() string {
	return e.GoaErrorName()
}

// GoaErrorName returns "HTTPError".
func (e *HTTPError) GoaErrorName() string {
	return "BadRequest"
}
