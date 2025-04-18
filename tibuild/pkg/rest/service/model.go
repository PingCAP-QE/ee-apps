package service

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"time"
)

type Context interface {
	context.Context
}

type Product string

const (
	ProductTidb             Product = "tidb"
	ProductEnterprisePlugin Product = "enterprise-plugin"
	ProductTikv             Product = "tikv"
	ProductPd               Product = "pd"
	ProductTiflash          Product = "tiflash"
	ProductBr               Product = "br"
	ProductDumpling         Product = "dumpling"
	ProductTidbLightning    Product = "tidb-lightning"
	ProductTicdc            Product = "ticdc"
	ProductTicdcNewarch     Product = "ticdc-newarch"
	ProductDm               Product = "dm"
	ProductTidbBinlog       Product = "tidb-binlog"
	ProductTidbTools        Product = "tidb-tools"
	ProductNgMonitoring     Product = "ng-monitoring"
	ProductTidbDashboard    Product = "tidb-dashboard"
	ProductDrainer          Product = "drainer"
	ProductPump             Product = "pump"

	ProductUnknown Product = ""
)

func (p Product) IsValid() bool {
	for _, v := range allProducts {
		if p == v {
			return true
		}
	}
	return false
}

type BranchCreateReq struct {
	Prod        Product `json:"prod"`
	BaseVersion string  `json:"baseVersion"`
}

type BranchCreateResp struct {
	Branch    string `json:"branch"`
	BranchURL string `json:"branchURL"`
}

type TagCreateReq struct {
	Prod   Product `json:"prod"`
	Branch string  `json:"branch"`
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

var allProducts = [...]Product{ProductTidb, ProductTikv, ProductPd,
	ProductTiflash, ProductBr, ProductTidbLightning, ProductDumpling,
	ProductTicdc, ProductTicdcNewarch, ProductDm, ProductTidbBinlog, ProductTidbTools,
	ProductNgMonitoring, ProductTidbDashboard, ProductDrainer, ProductPump}

func StringToProduct(s string) Product {
	for _, i := range allProducts {
		if s == string(i) {
			return Product(s)
		}
	}
	return ProductUnknown
}

func ProdToRepo(prod Product) *GithubRepo {
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
	Product           Product        `json:"product"`
	GitRef            string         `json:"gitRef"`
	GitHash           string         `json:"gitHash,omitempty" gorm:"type:varchar(64)"`
	Version           string         `json:"version"`
	Edition           ProductEdition `json:"edition"`
	Platform          string         `json:"platform,omitempty"` // "linux/amd64" or "linux/arm64" or "darwin/amd64" or "darwin/arm64" or empty for all platforms.
	PluginGitRef      string         `json:"pluginGitRef,omitempty"`
	BuildEnv          string         `json:"buildEnv,omitempty" gorm:"type:varchar(256)"`
	ProductDockerfile string         `json:"productDockerfile,omitempty" gorm:"type:varchar(256)"`
	ProductBaseImg    string         `json:"productBaseImg,omitempty" gorm:"type:varchar(256)"`
	BuilderImg        string         `json:"builderImg,omitempty" gorm:"type:varchar(256)"`
	GithubRepo        string         `json:"githubRepo,omitempty" gorm:"type:varchar(128)"`
	IsPushGCR         bool           `json:"isPushGCR,omitempty"`
	Features          string         `json:"features,omitempty" gorm:"type:varchar(128)"`
	IsHotfix          bool           `json:"isHotfix,omitempty"`
	TargetImg         string         `json:"targetImg,omitempty" gorm:"type:varchar(256)"`
	PipelineEngine    PipelineEngine `json:"pipelineEngine,omitempty" gorm:"type:varchar(16)"`
	prNumber          int
	prBaseRef         string
}

type PipelineEngine string

const (
	JenkinsEngine PipelineEngine = "jenkins"
	TektonEngine  PipelineEngine = "tekton"
)

type GitRef string

type ProductEdition string

const (
	EnterpriseEdition ProductEdition = "enterprise"
	CommunityEdition  ProductEdition = "community"
)

func (p ProductEdition) IsValid() bool {
	switch p {
	case EnterpriseEdition, CommunityEdition:
		return true
	default:
		return false
	}
}

type BuildStatus string

const (
	BuildStatusPending    BuildStatus = "PENDING"
	BuildStatusProcessing BuildStatus = "PROCESSING"
	BuildStatusAborted    BuildStatus = "ABORTED"
	BuildStatusSuccess    BuildStatus = "SUCCESS"
	BuildStatusFailure    BuildStatus = "FAILURE"
	BuildStatusError      BuildStatus = "ERROR"
)

func (p BuildStatus) IsValid() bool {
	switch p {
	case BuildStatusPending, BuildStatusProcessing, BuildStatusAborted, BuildStatusSuccess, BuildStatusFailure, BuildStatusError:
		return true
	default:
		return false
	}
}

func (p BuildStatus) IsCompleted() bool {
	switch p {
	case BuildStatusPending, BuildStatusProcessing:
		return false
	default:
		return true
	}
}

type DevBuildStatus struct {
	Status           BuildStatus     `json:"status" gorm:"type:varchar(16)"`
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
	Status       BuildStatus     `json:"status"`
	Platform     Platform        `json:"platform,omitempty"`
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
	Platform Platform `json:"platform"`
	URL      string   `json:"url"`
}

type Platform string

var (
	MultiArch   Platform = "multi-arch"
	LinuxAmd64  Platform = "linux/amd64"
	LinuxArm64  Platform = "linux/arm64"
	DarwinAmd64 Platform = "darwin/amd64"
	DarwinArm64 Platform = "darwin/arm64"
)

type BinArtifact struct {
	Component     string   `json:"component,omitempty"`
	Platform      Platform `json:"platform"`
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

type TibuildCtxKey string

var KeyOfApiAccount TibuildCtxKey = "apiAccount"

const AdminApiAccount = "admin"
const TibuildApiAccount = "tibuild"
