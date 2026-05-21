package handler

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/Masterminds/sprig/v3"
	"github.com/google/go-github/v68/github"
	"gopkg.in/yaml.v3"

	_ "embed"
)

const imageTagArtifactName = "result.json"

//go:embed imagetag_poll.md.tmpl
var imageTagPollResponseTmpl string

type imageTagPollParams struct {
	RunID int64
}

type imageTagPollResult struct {
	ImageRef  string         `json:"image_ref"`
	CreatedAt string         `json:"created_at"`
	Digest    string         `json:"digest"`
	MultiArch bool           `json:"multi_arch"`
	Platforms []string       `json:"platforms"`
	Labels    map[string]any `json:"labels"`
}

func runCommandImageTagPoll(ctx context.Context, args []string) (string, error) {
	if len(args) > 0 && (args[0] == "--help" || args[0] == "-h") {
		return imageTagHelpText, NewInformationError("Requested command usage")
	}

	params, err := parseCommandImageTagPoll(args)
	if err != nil {
		return "", err
	}

	cfg, token, err := loadImageTagWorkflowConfig(ctx)
	if err != nil {
		return "", err
	}

	gc := github.NewClient(nil).WithAuthToken(token)
	return pollImageTagWorkflow(ctx, gc, cfg, params.RunID, http.DefaultClient)
}

func parseCommandImageTagPoll(args []string) (*imageTagPollParams, error) {
	if len(args) != 1 {
		return nil, errors.New(imageTagHelpText)
	}

	runID, err := strconv.ParseInt(strings.TrimSpace(args[0]), 10, 64)
	if err != nil || runID <= 0 {
		return nil, fmt.Errorf("invalid run id %q", args[0])
	}

	return &imageTagPollParams{RunID: runID}, nil
}

func pollImageTagWorkflow(ctx context.Context, gc *github.Client, cfg imageTagWorkflowConfig, runID int64, httpClient *http.Client) (string, error) {
	run, _, err := gc.Actions.GetWorkflowRunByID(ctx, cfg.Owner, cfg.Repo, runID)
	if err != nil {
		return "", fmt.Errorf("poll image-tag workflow failed: %w", err)
	}

	runURL := buildImageTagRunURL(cfg, runID, run.GetHTMLURL())
	if run.GetStatus() != "completed" {
		return fmt.Sprintf("workflow run %d status: %s\nrun: %s", runID, run.GetStatus(), runURL), nil
	}

	if run.GetConclusion() != "success" {
		return "", fmt.Errorf("workflow run %d completed with conclusion %q\nrun: %s", runID, run.GetConclusion(), runURL)
	}

	result, err := downloadImageTagPollResult(ctx, gc, cfg, runID, httpClient)
	if err != nil {
		return "", err
	}

	return renderImageTagPollResponse(result)
}

func downloadImageTagPollResult(ctx context.Context, gc *github.Client, cfg imageTagWorkflowConfig, runID int64, httpClient *http.Client) (*imageTagPollResult, error) {
	artifact, err := getImageTagArtifact(ctx, gc, cfg, runID)
	if err != nil {
		return nil, err
	}

	downloadURL, _, err := gc.Actions.DownloadArtifact(ctx, cfg.Owner, cfg.Repo, artifact.GetID(), 0)
	if err != nil {
		return nil, fmt.Errorf("download result artifact redirect failed: %w", err)
	}
	if downloadURL == nil {
		return nil, fmt.Errorf("download result artifact redirect returned no URL")
	}

	archiveBytes, err := fetchImageTagArtifactArchive(ctx, httpClient, downloadURL.String())
	if err != nil {
		return nil, err
	}

	resultBytes, err := extractResultJSONFromArtifact(archiveBytes, imageTagArtifactName)
	if err != nil {
		return nil, err
	}

	var result imageTagPollResult
	if err := json.Unmarshal(resultBytes, &result); err != nil {
		return nil, fmt.Errorf("decode result.json failed: %w", err)
	}

	if result.Labels == nil {
		result.Labels = map[string]any{}
	}

	return &result, nil
}

func getImageTagArtifact(ctx context.Context, gc *github.Client, cfg imageTagWorkflowConfig, runID int64) (*github.Artifact, error) {
	artifacts, _, err := gc.Actions.ListWorkflowRunArtifacts(ctx, cfg.Owner, cfg.Repo, runID, &github.ListOptions{PerPage: 100})
	if err != nil {
		return nil, fmt.Errorf("list workflow run artifacts failed: %w", err)
	}
	if artifacts == nil {
		return nil, fmt.Errorf("workflow run %d returned no artifacts payload", runID)
	}

	for _, artifact := range artifacts.Artifacts {
		if artifact == nil {
			continue
		}
		if artifact.GetName() != imageTagArtifactName || artifact.GetExpired() {
			continue
		}
		return artifact, nil
	}

	return nil, fmt.Errorf("workflow run %d did not produce a %s artifact", runID, imageTagArtifactName)
}

func fetchImageTagArtifactArchive(ctx context.Context, httpClient *http.Client, archiveURL string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, archiveURL, nil)
	if err != nil {
		return nil, fmt.Errorf("build artifact download request failed: %w", err)
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("download artifact archive failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download artifact archive failed: unexpected status %s", resp.Status)
	}

	archiveBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read artifact archive failed: %w", err)
	}

	return archiveBytes, nil
}

func extractResultJSONFromArtifact(archiveBytes []byte, filename string) ([]byte, error) {
	reader, err := zip.NewReader(bytes.NewReader(archiveBytes), int64(len(archiveBytes)))
	if err != nil {
		return nil, fmt.Errorf("open artifact archive failed: %w", err)
	}

	for _, file := range reader.File {
		if file == nil {
			continue
		}
		if file.Name != filename && !strings.HasSuffix(file.Name, "/"+filename) {
			continue
		}

		rc, err := file.Open()
		if err != nil {
			return nil, fmt.Errorf("open %s from artifact failed: %w", filename, err)
		}

		body, readErr := io.ReadAll(rc)
		closeErr := rc.Close()
		if readErr != nil {
			return nil, fmt.Errorf("read %s from artifact failed: %w", filename, readErr)
		}
		if closeErr != nil {
			return nil, fmt.Errorf("close %s from artifact failed: %w", filename, closeErr)
		}

		return body, nil
	}

	return nil, fmt.Errorf("artifact archive does not contain %s", filename)
}

func renderImageTagPollResponse(result *imageTagPollResult) (string, error) {
	tmpl := template.Must(template.New("markdown").
		Funcs(sprig.FuncMap()).
		Funcs(template.FuncMap{
			"toYaml": func(v any) string {
				yamlBytes, err := yaml.Marshal(v)
				if err != nil {
					return fmt.Sprintf("failed to marshal to YAML: %v", err)
				}
				return strings.TrimSuffix(string(yamlBytes), "\n")
			},
		}).
		Parse(imageTagPollResponseTmpl))

	data := map[string]any{
		"image_ref":  result.ImageRef,
		"created_at": result.CreatedAt,
		"digest":     result.Digest,
		"multi_arch": result.MultiArch,
		"platforms":  result.Platforms,
		"labels":     result.Labels,
	}

	var sb strings.Builder
	if err := tmpl.Execute(&sb, data); err != nil {
		return "", fmt.Errorf("render image-tag reply failed: %w", err)
	}

	return sb.String(), nil
}
