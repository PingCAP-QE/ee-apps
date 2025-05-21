// Package service provides constants, types, and utilities for handling build services,
package service

import "slices"

// including definitions for engines, platforms, editions, products, and build status.
// It also contains mappings between product names and their respective GitHub repositories.

const (
	// Engines
	JenkinsEngine = "jenkins"
	TektonEngine  = "tekton"

	// Platforms
	MultiArch   = "multi-arch"
	LinuxAmd64  = "linux/amd64"
	LinuxArm64  = "linux/arm64"
	DarwinAmd64 = "darwin/amd64"
	DarwinArm64 = "darwin/arm64"

	// Editions
	EditionEnterprise = "enterprise"
	EditionCommunity  = "community"
	EditionFailPoint  = "failpoint"
	EditionFips       = "fips"
	EditionNextGen    = "next-gen"
	EditionExperiment = "experiment"

	// Products
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
	ProductUnknown          = "" // unkown

	// Build status
	BuildStatusPending    = "PENDING"
	BuildStatusProcessing = "PROCESSING"
	BuildStatusAborted    = "ABORTED"
	BuildStatusSuccess    = "SUCCESS"
	BuildStatusFailure    = "FAILURE"
	BuildStatusError      = "ERROR"
)

var (
	// editions
	InvalidEditionForJenkins = []string{EditionEnterprise, EditionCommunity}
	InvalidEditionForTekton  = []string{EditionEnterprise, EditionCommunity, EditionFailPoint, EditionFips, EditionExperiment, EditionNextGen}

	// build status
	validBuildStatuses = []string{BuildStatusPending, BuildStatusProcessing, BuildStatusAborted, BuildStatusSuccess, BuildStatusFailure, BuildStatusError}

	// products
	allProducts = []string{
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

	// code repositoies
	RepoNgMonitoring  = GithubRepo{Owner: "pingcap", Repo: "ng-monitoring"}
	RepoTicdc         = GithubRepo{Owner: "pingcap", Repo: "ticdc"}
	RepoTidb          = GithubRepo{Owner: "pingcap", Repo: "tidb"}
	RepoTidbBinlog    = GithubRepo{Owner: "pingcap", Repo: "tidb-binlog"}
	RepoTidbDashboard = GithubRepo{Owner: "pingcap", Repo: "tidb-dashboard"}
	RepoTidbTools     = GithubRepo{Owner: "pingcap", Repo: "tidb-tools"}
	RepoTiflash       = GithubRepo{Owner: "pingcap", Repo: "tiflash"}
	RepoTiflow        = GithubRepo{Owner: "pingcap", Repo: "tiflow"}
	RepoTiproxy       = GithubRepo{Owner: "pingcap", Repo: "tiproxy"}
	RepoTikv          = GithubRepo{Owner: "tikv", Repo: "tikv"}
	RepoPd            = GithubRepo{Owner: "tikv", Repo: "pd"}

	// product name to code repository mapping.
	prodToRepoMap = map[string]*GithubRepo{
		ProductBr:            &RepoTidb,
		ProductDm:            &RepoTiflow,
		ProductDrainer:       &RepoTidbBinlog,
		ProductDumpling:      &RepoTidb,
		ProductNgMonitoring:  &RepoNgMonitoring,
		ProductPd:            &RepoPd,
		ProductPump:          &RepoTidbBinlog,
		ProductTicdc:         &RepoTiflow,
		ProductTicdcNewarch:  &RepoTicdc,
		ProductTidb:          &RepoTidb,
		ProductTidbBinlog:    &RepoTidbBinlog,
		ProductTidbDashboard: &RepoTidbDashboard,
		ProductTidbLightning: &RepoTidb,
		ProductTidbTools:     &RepoTidbTools,
		ProductTiflash:       &RepoTiflash,
		ProductTikv:          &RepoTikv,
		ProductTiproxy:       &RepoTiproxy,
	}
)

func StringToProduct(s string) string {
	if slices.Contains(allProducts, s) {
		return s
	}
	return ProductUnknown
}

func IsValidBuildStatus(status string) bool {
	return slices.Contains(validBuildStatuses, status)
}

func IsBuildStatusCompleted(status string) bool {
	return !slices.Contains([]string{BuildStatusPending, BuildStatusProcessing}, status)
}
