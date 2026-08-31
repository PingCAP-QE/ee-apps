package handler

import (
	"context"
	"flag"
	"fmt"
	"strings"

	"github.com/go-resty/resty/v2"
)

type triggerParams struct {
	product           string
	edition           string
	version           string
	platform          string
	gitRef            string
	pluginGitRef      string
	githubRepo        string
	features          string
	productDockerfile string
	productBaseImg    string
	builderImg        string
	targetImg         string
	engine            string
	buildEnvs         []string
	hotfix            bool
	pushGCR           bool
	dryRun            bool
}

type devbuildCreateRequest struct {
	CreatedBy string            `json:"createdBy"`
	Request   devbuildSpecInput `json:"request"`
}

type devbuildSpecInput struct {
	Product           string `json:"product"`
	Edition           string `json:"edition"`
	Version           string `json:"version"`
	Platform          string `json:"platform,omitempty"`
	GitRef            string `json:"gitRef"`
	PluginGitRef      string `json:"pluginGitRef,omitempty"`
	GithubRepo        string `json:"githubRepo,omitempty"`
	Features          string `json:"features,omitempty"`
	IsHotfix          bool   `json:"isHotfix,omitempty"`
	BuildEnv          string `json:"buildEnv,omitempty"`
	ProductDockerfile string `json:"productDockerfile,omitempty"`
	ProductBaseImg    string `json:"productBaseImg,omitempty"`
	BuilderImg        string `json:"builderImg,omitempty"`
	TargetImg         string `json:"targetImg,omitempty"`
	PipelineEngine    string `json:"pipelineEngine,omitempty"`
}

// Verify required params
func (p triggerParams) Verify() error {
	missingFlags := []string{}
	if p.product == "" {
		missingFlags = append(missingFlags, "--product")
	}
	if p.version == "" {
		missingFlags = append(missingFlags, "--version")
	}
	if p.gitRef == "" {
		missingFlags = append(missingFlags, "--gitRef")
	}
	if len(missingFlags) > 0 {
		return fmt.Errorf("required flags %v are missing", missingFlags)
	}

	return nil
}

type triggerResult struct {
	ID int `json:"id"`
}

type arrayFlags []string

func (i *arrayFlags) String() string {
	return strings.Join(*i, ",")
}

func (i *arrayFlags) Set(value string) error {
	*i = append(*i, value)
	return nil
}

func runCommandDevbuildTrigger(ctx context.Context, args []string) (string, error) {
	// Get API URL from context
	apiURL := ctx.Value(cfgKeyDevBuildURL).(string)
	if apiURL == "" {
		return "", fmt.Errorf("API URL not found in context")
	}

	params, err := parseCommandDevbuildTrigger(args)
	if err != nil {
		return "", err
	}

	triggerRequest := devbuildCreateRequest{
		CreatedBy: ctx.Value(ctxKeyLarkSenderEmail).(string),
		Request: devbuildSpecInput{
			Product:           params.product,
			Edition:           params.edition,
			Version:           params.version,
			Platform:          params.platform,
			GitRef:            params.gitRef,
			PluginGitRef:      params.pluginGitRef,
			GithubRepo:        params.githubRepo,
			Features:          params.features,
			IsHotfix:          params.hotfix,
			BuildEnv:          strings.Join(params.buildEnvs, " "),
			ProductDockerfile: params.productDockerfile,
			ProductBaseImg:    params.productBaseImg,
			BuilderImg:        params.builderImg,
			TargetImg:         params.targetImg,
			PipelineEngine:    params.engine,
		},
	}

	client := resty.New()
	resp, err := client.R().
		SetResult(triggerResult{}).
		SetBody(triggerRequest).
		SetQueryParam("dryrun", fmt.Sprint(params.dryRun)).
		// TODO: add auth in header.
		Post(apiURL)
	if err != nil {
		return "", err
	}
	if !resp.IsSuccess() {
		return "", fmt.Errorf("trigger devbuild failed: %s", resp.String())
	}

	result := resp.Result().(*triggerResult)

	return fmt.Sprintf("build id is %d", result.ID), nil
}

func parseCommandDevbuildTrigger(args []string) (*triggerParams, error) {
	var ret triggerParams
	var buildEnv arrayFlags

	fs := flag.NewFlagSet("trigger", flag.ContinueOnError)
	{
		// Required flags (previously positional arguments)
		fs.StringVar(&ret.product, "product", "", "product to build (required)")
		fs.StringVar(&ret.version, "version", "", "version to build (required)")
		fs.StringVar(&ret.gitRef, "gitRef", "", "git reference to build from (required)")

		// Optional flags
		fs.StringVar(&ret.edition, "e", "community", "default is community")
		fs.StringVar(&ret.edition, "edition", "community", "default is community")
		fs.StringVar(&ret.platform, "p", "", "platform to build, default is for all")
		fs.StringVar(&ret.platform, "platform", "", "platform to build, default is for all")
		fs.StringVar(&ret.pluginGitRef, "pluginGitRef", "", "only for build enterprise tidb, ignore if you dont know")
		// Keep accepting this legacy option, but do not expose or forward it.
		fs.BoolVar(&ret.pushGCR, "pushGCR", false, "")
		fs.BoolVar(&ret.hotfix, "hotfix", false, "")
		fs.StringVar(&ret.githubRepo, "githubRepo", "", "only for the forked github repo")
		fs.StringVar(&ret.features, "features", "", "build features, eg failpoint")
		fs.BoolVar(&ret.dryRun, "dryrun", false, "dry run if you want to test")
		fs.Var(&buildEnv, "buildEnv", "build environment")
		fs.StringVar(&ret.productDockerfile, "productDockerfile", "", "dockerfile url for product")
		fs.StringVar(&ret.productBaseImg, "productBaseImg", "", "product base image")
		fs.StringVar(&ret.builderImg, "builderImg", "", "specify docker image for builder")
		fs.StringVar(&ret.targetImg, "targetImg", "", "")
		fs.StringVar(&ret.engine, "engine", "", "pipeline engine")
	}

	if err := fs.Parse(args); err != nil {
		return nil, NewInformationError(devBuildHelpText)
	}

	if ret.engine == "" {
		ret.engine = defaultEngineForProduct(ret.product)
	}

	if err := ret.Verify(); err != nil {
		return nil, err
	}

	ret.buildEnvs = buildEnv

	return &ret, nil
}

func defaultEngineForProduct(product string) string {
	return "tekton"
}
