package handler

import (
	"context"
	"flag"
	"fmt"
	"html/template"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/Masterminds/sprig/v3"
	"github.com/go-resty/resty/v2"
	"gopkg.in/yaml.v3"

	_ "embed"
)

//go:embed devbuild_poll.md.tmpl
var devBuildPollResponseTmpl string

const loopPollInterval = 10 * time.Second

type pollParams struct {
	buildID string
}

type pollResult struct {
	Status struct {
		Status           string         `json:"status,omitempty"`
		PipelineViewURL  string         `json:"pipelineViewURL,omitempty"`
		PipelineViewURLs []string       `json:"pipelineViewURLs,omitempty"`
		BuildReport      map[string]any `json:"buildReport,omitempty"`
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

func runCommandDevbuildPoll(_ context.Context, args []string) (string, error) {
	params, err := parseCommandDevbuildPoll(args)
	if err != nil {
		return "", fmt.Errorf("failed to parse poll command: %v", err)
	}

	result, err := pollDevbuildStatus(params.buildID)
	if err != nil {
		return "", err
	}

	return renderDevbuildStatusForLark(result)
}

func renderDevbuildStatusForLark(result *pollResult) (string, error) {
	t := template.Must(template.New("markdown").
		Funcs(sprig.FuncMap()).
		Funcs(template.FuncMap{"toYaml": func(v any) string {
			yamlBytes, err := yaml.Marshal(v)
			if err != nil {
				return fmt.Sprintf("failed to marshal to YAML: %v", err)
			}
			return strings.TrimSuffix(string(yamlBytes), "\n")
		}}).
		Parse(devBuildPollResponseTmpl))

	// Execute the template with the result data
	var sb strings.Builder
	if err := t.Execute(&sb, result); err != nil {
		return "", fmt.Errorf("failed to execute template: %v", err)
	}

	return sb.String(), nil
}

func loopPollDevbuildStatus(buildID string) (*pollResult, error) {
	for {
		result, err := pollDevbuildStatus(buildID)
		if err != nil {
			return nil, err
		}
		if slices.Contains([]string{"ABORTED", "SUCCESS", "FAILURE", "ERROR"}, result.Status.Status) {
			return result, nil
		}
		time.Sleep(loopPollInterval)
	}
}

func pollDevbuildStatus(buildID string) (*pollResult, error) {
	client := resty.New()
	reqUrl, err := url.JoinPath(devBuildURL, buildID)
	if err != nil {
		return nil, err
	}

	req := client.R().
		SetResult(pollResult{})

	resp, err := req.Get(reqUrl)
	if err != nil {
		return nil, err
	}
	if !resp.IsSuccess() {
		return nil, fmt.Errorf("poll devbuild failed: %s", resp.String())
	}
	result := resp.Result().(*pollResult)
	return result, nil
}
