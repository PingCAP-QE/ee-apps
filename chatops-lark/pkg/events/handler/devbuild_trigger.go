package handler

import (
	"flag"
	"fmt"
	"strings"

	"github.com/go-resty/resty/v2"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type triggerParams struct {
	product           string
	edition           string
	version           string
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

func runCommandDevbuildTrigger(args []string, sender *CommandSender) (string, error) {
	params, err := parseCommandDevbuildTrigger(args)
	if err != nil {
		return "", fmt.Errorf("failed to parse trigger command: %v", err)
	}

	triggerParams := service.DevBuild{
		Meta: service.DevBuildMeta{
			CreatedBy: sender.Email,
		},
		Spec: service.DevBuildSpec{
			Product:           service.StringToProduct(params.product),
			Edition:           service.ProductEdition(params.edition),
			Version:           params.version,
			GitRef:            params.gitRef,
			PluginGitRef:      params.pluginGitRef,
			GithubRepo:        params.githubRepo,
			IsPushGCR:         params.pushGCR,
			Features:          params.features,
			IsHotfix:          params.hotfix,
			BuildEnv:          strings.Join(params.buildEnvs, " "),
			ProductDockerfile: params.productDockerfile,
			ProductBaseImg:    params.productBaseImg,
			BuilderImg:        params.builderImg,
			TargetImg:         params.targetImg,
			PipelineEngine:    service.PipelineEngine(params.engine),
		},
	}

	client := resty.New()
	resp, err := client.R().
		SetResult(triggerResult{}).
		SetBody(triggerParams).
		SetQueryParam("dryrun", fmt.Sprint(params.dryRun)).
		// TODO: add auth in header.
		Post(devBuildURL)
	if err != nil {
		return "", err
	}
	if !resp.IsSuccess() {
		return "", fmt.Errorf("trigger devbuild failed: %s", resp.String())
	}

	result := resp.Result().(*triggerResult)

	return fmt.Sprintf("build id is %d\npolling: %s/%d", result.ID, devBuildURL, result.ID), nil
}

func parseCommandDevbuildTrigger(args []string) (*triggerParams, error) {
	var ret triggerParams
	var buildEnv arrayFlags

	fs := flag.NewFlagSet("trigger", flag.ContinueOnError)
	{
		fs.StringVar(&ret.edition, "e", "community", "default is community")
		fs.StringVar(&ret.edition, "edition", "community", "default is community")
		fs.StringVar(&ret.pluginGitRef, "pluginGitRef", "", "only for build enterprise tidb, ignore if you dont know")
		fs.BoolVar(&ret.pushGCR, "pushGCR", false, "whether to push GCR, default is no")
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
		return nil, err
	}

	if fs.NArg() < 3 {
		return nil, fmt.Errorf("missing required positional arguments: product, version, gitRef")
	}

	ret.product = fs.Arg(0)
	ret.version = fs.Arg(1)
	ret.gitRef = fs.Arg(2)
	ret.buildEnvs = buildEnv

	return &ret, nil
}
