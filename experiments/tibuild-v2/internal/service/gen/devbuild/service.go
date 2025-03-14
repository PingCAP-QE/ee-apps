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
var MethodNames = [5]string{"list", "create", "get", "update", "rerun"}

type BinArtifact struct {
	Component     string
	OciFile       *OciFile
	Platform      string
	Sha256OciFile *OciFile
	Sha256URL     string
	URL           string
}

type BuildReport struct {
	Binaries       []*BinArtifact
	GitHash        string
	Images         []*ImageArtifact
	PluginGitHash  string
	PrintedVersion string
}

type BuildStatus string

// CreatePayload is the payload type of the devbuild service create method.
type CreatePayload struct {
	// Creator of build
	CreatedBy string
	// Build to create, only spec field is required, others are ignored
	Request *DevBuildRequest
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

type DevBuildRequest struct {
	BuildEnv          *string
	BuilderImg        *string
	Edition           ProductEdition
	Features          *string
	GitRef            string
	GithubRepo        *string
	IsHotfix          *bool
	IsPushGcr         *bool
	PipelineEngine    *PipelineEngine
	PluginGitRef      *string
	Product           Product
	ProductBaseImg    *string
	ProductDockerfile *string
	TargetImg         *string
	Version           string
}

type DevBuildSpec struct {
	BuildEnv          string
	BuilderImg        string
	Edition           ProductEdition
	Features          string
	GitHash           string
	GitRef            string
	GithubRepo        string
	IsHotfix          bool
	IsPushGcr         bool
	PipelineEngine    PipelineEngine
	PluginGitRef      string
	Product           Product
	ProductBaseImg    string
	ProductDockerfile string
	TargetImg         string
	Version           string
}

type DevBuildStatus struct {
	BuildReport      *BuildReport
	ErrMsg           string
	PipelineBuildID  int
	PipelineEndAt    string
	PipelineStartAt  string
	PipelineViewURL  string
	PipelineViewURLs []string
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

type PipelineEngine string

type Product string

type ProductEdition string

// RerunPayload is the payload type of the devbuild service rerun method.
type RerunPayload struct {
	// ID of build
	ID int
	// Dry run
	Dryrun bool
}

type TektonPipeline struct {
	EndAt        string
	GitHash      string
	Images       []*ImageArtifact
	Name         string
	OciArtifacts []*OciArtifact
	Platform     string
	StartAt      string
	Status       BuildStatus
	URL          string
}

type TektonStatus struct {
	Pipelines []*TektonPipeline
}

// UpdatePayload is the payload type of the devbuild service update method.
type UpdatePayload struct {
	// ID of build
	ID int
	// Build to update
	DevBuild *DevBuild
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
