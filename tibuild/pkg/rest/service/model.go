package service

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"
)

const (
	ProductBr               = "br"
	ProductDm               = "dm"
	ProductDrainer          = "drainer"
	ProductDumpling         = "dumpling"
	ProductEnterprisePlugin = "enterprise-plugin"
	ProductNgMonitoring     = "ng-monitoring"
	ProductPd               = "pd"
	ProductPump             = "pump"
	ProductTicdc            = "ticdc"
	ProductTicdcNewarch     = "ticdc-newarch"
	ProductTidb             = "tidb"
	ProductTidbBinlog       = "tidb-binlog"
	ProductTidbDashboard    = "tidb-dashboard"
	ProductTidbLightning    = "tidb-lightning"
	ProductTidbTools        = "tidb-tools"
	ProductTiflash          = "tiflash"
	ProductTikv             = "tikv"
	ProductTiproxy          = "tiproxy"

	ProductUnknown = ""
)

const (
	KeyOfApiAccount   = "apiAccount"
	AdminApiAccount   = "admin"
	TibuildApiAccount = "tibuild"
)

type BranchCreateReq struct {
	Prod        string `json:"prod"`
	BaseVersion string `json:"baseVersion"`
}

type BranchCreateResp struct {
	Branch    string `json:"branch"`
	BranchURL string `json:"branchURL"`
}

type TagCreateReq struct {
	Prod   string `json:"prod"`
	Branch string `json:"branch"`
}

type TagCreateResp struct {
	Tag    string `json:"tag"`
	TagURL string `json:"tagURL"`
}

type GithubRepo struct {
	Owner string
	Repo  string
}

func (r GithubRepo) URL() string {
	return fmt.Sprintf("https://github.com/%s/%s", r.Owner, r.Repo)
}

func GHRepoToStruct(repo string) *GithubRepo {
	ss := strings.Split(repo, "/")
	if len(ss) != 2 {
		return nil
	}
	return &GithubRepo{Owner: ss[0], Repo: ss[1]}
}

var (
	RepoTidb          = GithubRepo{Owner: "pingcap", Repo: "tidb"}
	RepoTikv          = GithubRepo{Owner: "tikv", Repo: "tikv"}
	RepoPd            = GithubRepo{Owner: "tikv", Repo: "pd"}
	RepoTiflash       = GithubRepo{Owner: "pingcap", Repo: "tiflash"}
	RepoTiflow        = GithubRepo{Owner: "pingcap", Repo: "tiflow"}
	RepoTicdc         = GithubRepo{Owner: "pingcap", Repo: "ticdc"}
	RepoTidbBinlog    = GithubRepo{Owner: "pingcap", Repo: "tidb-binlog"}
	RepoTidbTools     = GithubRepo{Owner: "pingcap", Repo: "tidb-tools"}
	RepoNgMonitoring  = GithubRepo{Owner: "pingcap", Repo: "ng-monitoring"}
	RepoTidbDashboard = GithubRepo{Owner: "pingcap", Repo: "tidb-dashboard"}
)

var allProducts = []string{
	ProductBr,
	ProductDm,
	ProductDrainer,
	ProductDumpling,
	ProductNgMonitoring,
	ProductPd,
	ProductPump,
	ProductTicdc,
	ProductTicdcNewarch,
	ProductTidb,
	ProductTidbBinlog,
	ProductTidbDashboard,
	ProductTidbLightning,
	ProductTidbTools,
	ProductTiflash,
	ProductTikv,
	ProductTiproxy,
}

func StringToProduct(s string) string {
	if slices.Contains(allProducts, s) {
		return s
	}
	return ProductUnknown
}

func ProdToRepo(prod string) *GithubRepo {
	switch prod {
	case ProductBr, ProductTidbLightning, ProductDumpling, ProductTidb:
		return &RepoTidb
	case ProductTikv:
		return &RepoTikv
	case ProductPd:
		return &RepoPd
	case ProductTiflash:
		return &RepoTiflash
	case ProductTicdc, ProductDm:
		return &RepoTiflow
	case ProductTicdcNewarch:
		return &RepoTicdc
	case ProductDrainer, ProductPump:
		fallthrough
	case ProductTidbBinlog:
		return &RepoTidbBinlog
	case ProductTidbTools:
		return &RepoTidbTools
	case ProductNgMonitoring:
		return &RepoNgMonitoring
	case ProductTidbDashboard:
		return &RepoTidbDashboard
	default:
		return nil
	}
}

type DevBuild struct {
	ID     int            `json:"id" gorm:"primaryKey"`
	Meta   DevBuildMeta   `json:"meta" gorm:"embedded"`
	Spec   DevBuildSpec   `json:"spec" gorm:"embedded"`
	Status DevBuildStatus `json:"status" gorm:"embedded"`
}

type DevBuildMeta struct {
	CreatedAt time.Time `json:"createdAt"`
	CreatedBy string    `json:"createdBy" gorm:"type:varchar(32)"`
	UpdatedAt time.Time `json:"updatedAt"`
}

type DevBuildListOption struct {
	Offset    uint    `form:"offset"`
	Size      uint    `form:"size"`
	Hotfix    *bool   `form:"hotfix"`
	CreatedBy *string `form:"createdBy"`
}

type DevBuildGetOption struct {
	Sync bool `form:"sync"`
}

type DevBuildSaveOption struct {
	DryRun bool `form:"dryrun"`
}

type DevBuildSpec struct {
	Product           string `json:"product"`
	GitRef            string `json:"gitRef"`
	GitHash           string `json:"gitHash,omitempty" gorm:"type:varchar(64)"`
	Version           string `json:"version"`
	Edition           string `json:"edition"`
	Platform          string `json:"platform,omitempty"` // "linux/amd64" or "linux/arm64" or "darwin/amd64" or "darwin/arm64" or empty for all platforms.
	PluginGitRef      string `json:"pluginGitRef,omitempty"`
	BuildEnv          string `json:"buildEnv,omitempty" gorm:"type:varchar(256)"`
	ProductDockerfile string `json:"productDockerfile,omitempty" gorm:"type:varchar(256)"`
	ProductBaseImg    string `json:"productBaseImg,omitempty" gorm:"type:varchar(256)"`
	BuilderImg        string `json:"builderImg,omitempty" gorm:"type:varchar(256)"`
	GithubRepo        string `json:"githubRepo,omitempty" gorm:"type:varchar(128)"`
	IsPushGCR         bool   `json:"isPushGCR,omitempty"`
	Features          string `json:"features,omitempty" gorm:"type:varchar(128)"`
	IsHotfix          bool   `json:"isHotfix,omitempty"`
	TargetImg         string `json:"targetImg,omitempty" gorm:"type:varchar(256)"`
	PipelineEngine    string `json:"pipelineEngine,omitempty" gorm:"type:varchar(16)"`
	prNumber          int
	prBaseRef         string
}

const (
	JenkinsEngine = "jenkins"
	TektonEngine  = "tekton"
)

type GitRef string

// Edition constants define the valid values for DevBuildSpec.Edition
const (
	EditionEnterprise = "enterprise"
	EditionCommunity  = "community"
	EditionFailPoint  = "failpoint"
	EditionFips       = "fips"
	EditionExperiment = "experiment"
)

var (
	InvalidEditionForJenkins = []string{EditionEnterprise, EditionCommunity}
	InvalidEditionForTekton  = []string{EditionEnterprise, EditionCommunity, EditionFailPoint, EditionFips, EditionExperiment}
)

type BuildStatus string

const (
	BuildStatusPending    = "PENDING"
	BuildStatusProcessing = "PROCESSING"
	BuildStatusAborted    = "ABORTED"
	BuildStatusSuccess    = "SUCCESS"
	BuildStatusFailure    = "FAILURE"
	BuildStatusError      = "ERROR"
)

var validBuildStatuses = []string{BuildStatusPending, BuildStatusProcessing, BuildStatusAborted, BuildStatusSuccess, BuildStatusFailure, BuildStatusError}

func IsValidBuildStatus(status string) bool {
	return slices.Contains(validBuildStatuses, status)
}

func IsBuildStatusCompleted(status string) bool {
	return !slices.Contains([]string{BuildStatusPending, BuildStatusProcessing}, status)
}

type DevBuildStatus struct {
	Status           string          `json:"status" gorm:"type:varchar(16)"`
	PipelineBuildID  int64           `json:"pipelineBuildID,omitempty"`
	PipelineViewURL  string          `json:"pipelineViewURL,omitempty" gorm:"-"`
	PipelineViewURLs []string        `json:"pipelineViewURLs,omitempty" gorm:"-"`
	ErrMsg           string          `json:"errMsg,omitempty" gorm:"type:varchar(256)"`
	PipelineStartAt  *time.Time      `json:"pipelineStartAt,omitempty"`
	PipelineEndAt    *time.Time      `json:"pipelineEndAt,omitempty"`
	BuildReport      *BuildReport    `json:"buildReport,omitempty" gorm:"-:all"`
	BuildReportJson  json.RawMessage `json:"-" gorm:"column:build_report;type:json"`
	TektonStatus     *TektonStatus   `json:"tektonStatus,omitempty" gorm:"-:all"`
	TektonStatusJson json.RawMessage `json:"-" gorm:"column:tekton_status;type:json"`
}

type TektonStatus struct {
	Pipelines []TektonPipeline `json:"pipelines"`
}

type TektonPipeline struct {
	Name         string          `json:"name"`
	URL          string          `json:"url,omitempty"`
	GitHash      string          `json:"gitHash,omitempty"`
	Status       string          `json:"status"`
	Platform     string          `json:"platform,omitempty"`
	StartAt      *time.Time      `json:"startAt,omitempty"`
	EndAt        *time.Time      `json:"endAt,omitempty"`
	OciArtifacts []OciArtifact   `json:"ociArtifacts,omitempty"`
	Images       []ImageArtifact `json:"images,omitempty"`
}

type OciArtifact struct {
	Repo  string   `json:"repo"`
	Tag   string   `json:"tag"`
	Files []string `json:"files"`
}

type BuildReport struct {
	GitHash        string          `json:"gitHash"`
	PluginGitHash  string          `json:"pluginGitHash,omitempty"`
	Images         []ImageArtifact `json:"images,omitempty"`
	Binaries       []BinArtifact   `json:"binaries,omitempty"`
	PrintedVersion string          `json:"printedVersion,omitempty"`
}

type ImageArtifact struct {
	Platform string `json:"platform"`
	URL      string `json:"url"`
}

var (
	MultiArch   = "multi-arch"
	LinuxAmd64  = "linux/amd64"
	LinuxArm64  = "linux/arm64"
	DarwinAmd64 = "darwin/amd64"
	DarwinArm64 = "darwin/arm64"
)

type BinArtifact struct {
	Component     string   `json:"component,omitempty"`
	Platform      string   `json:"platform"`
	URL           string   `json:"url"`
	Sha256URL     string   `json:"sha256URL"`
	OciFile       *OciFile `json:"ociFile,omitempty"`
	Sha256OciFile *OciFile `json:"sha256OciFile,omitempty"`
}

type OciFile struct {
	Repo string `json:"repo"`
	Tag  string `json:"tag"`
	File string `json:"file"`
}

type ImageSyncRequest struct {
	Source string `json:"source"`
	Target string `json:"target"`
}
