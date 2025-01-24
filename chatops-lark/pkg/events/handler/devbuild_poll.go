package handler

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/url"

	"github.com/go-resty/resty/v2"
)

type pollParams struct {
	buildID string
}

type pollResult struct {
	Status struct {
		Status          string `json:"status"`
		PipelineViewUrl string `json:"pipelineViewURL"`
		BuildReport     string `json:"buildReport"`
	}
}

func parseCommandDevbuildPoll(args []string) (*pollParams, error) {
	var ret pollParams

	fs := flag.NewFlagSet("poll", flag.ContinueOnError)
	fs.Parse(args)
	if fs.NArg() < 1 {
		return nil, fmt.Errorf("missing required positional arguments: buildId")
	}

	ret.buildID = fs.Arg(0)

	return &ret, nil
}

func runCommandDevbuildPoll(args []string) (string, error) {
	params, err := parseCommandDevbuildPoll(args)
	if err != nil {
		return "", fmt.Errorf("failed to parse poll command: %v", err)
	}

	client := resty.New()
	reqUrl, err := url.JoinPath(devBuildURL, params.buildID)
	if err != nil {
		return "", err
	}

	resp, err := client.R().
		SetResult(pollResult{}).
		// TODO: add auth in header.
		Get(reqUrl)
	if err != nil {
		return "", err
	}
	if !resp.IsSuccess() {
		return "", fmt.Errorf("poll devbuild failed: %s", resp.String())
	}
	result := resp.Result().(*pollResult)

	resultBytes, _ := json.Marshal(result)
	return fmt.Sprintf("build status is %s", resultBytes), nil
}
