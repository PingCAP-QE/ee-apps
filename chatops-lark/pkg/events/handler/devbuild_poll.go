package handler

import (
	"context"
	"flag"
	"fmt"
	"html/template"
	"net/url"
	"strings"

	"github.com/Masterminds/sprig/v3"
	"github.com/go-resty/resty/v2"
	"gopkg.in/yaml.v3"

	_ "embed"
)

//go:embed devbuild_poll.md.tmpl
var devBuildPollResponseTmpl string

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

func runCommandDevbuildPoll(ctx context.Context, args []string) (string, error) {
	params, err := parseCommandDevbuildPoll(args)
	if err != nil {
		return "", fmt.Errorf("failed to parse poll command: %v", err)
	}

	// Get API URL from context
	apiURL := ctx.Value(cfgKeyDevBuildURL).(string)

	client := resty.New()
	reqUrl, err := url.JoinPath(apiURL, params.buildID)
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

	// Create a new template and add a custom function to format JSON
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
