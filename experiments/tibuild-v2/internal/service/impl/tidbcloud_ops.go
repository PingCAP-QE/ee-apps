package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strings"

	"golang.org/x/mod/semver"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

const (
	nextgenCalendarTagThreshold = "v26.0.0"
	v263FixedVersion            = "v26.3.99"

	updateComponentVersionAPIPath = "/devops/cloudconfig/versions/component/"
)

var (
	legacyNextgenImageTagRegexp         = regexp.MustCompile(`^v\d+\.\d+\.\d+-nextgen\.\d{6}\.\d+$`)
	calendarSemverNextgenImageTagRegexp = regexp.MustCompile(`^v2[6-9]\.\d+\.\d+(-nextgen)?$`)
	v263VersionRegexp                   = regexp.MustCompile(`^v26\.3\.\d+$`)
)

// UpdateComponentVersionInCloudconfig updates every TiDB Cloud component whose
// base image matches the pushed image.
func (s *tidbcloudsrvc) UpdateComponentVersionInCloudconfig(ctx context.Context, p *tidbcloud.UpdateComponentVersionInCloudconfigPayload) (*tidbcloud.UpdateComponentVersionInCloudconfigResult, error) {
	if p == nil {
		return nil, fmt.Errorf("payload is nil")
	}
	if strings.TrimSpace(p.Stage) == "" {
		return nil, fmt.Errorf("stage is empty")
	}
	if strings.TrimSpace(p.Image) == "" {
		return nil, fmt.Errorf("image is empty")
	}

	opsCfg := s.opsConfig()
	if opsCfg == nil {
		return nil, fmt.Errorf("tidbcloud ops config is not configured")
	}
	stageCfg, ok := opsCfg.Stages[p.Stage]
	if !ok {
		return nil, fmt.Errorf("stage %q not found in tidbcloud ops config", p.Stage)
	}

	imageRepo, imageTag, err := parseImageRepoTag(p.Image)
	if err != nil {
		s.logger.Error().Err(err).Str("stage", p.Stage).Str("image", p.Image).
			Msg("tidbcloud.update-component-version-in-cloudconfig failed to parse image")
		return nil, err
	}

	// Only next-gen release tags move cloud config; nightly/daily tags do not.
	if !isSupportedNextgenImageTag(imageTag) {
		s.logger.Info().Str("stage", p.Stage).Str("image", p.Image).Str("image_tag", imageTag).
			Msg("skip update: image_tag is not a supported nextgen release tag")
		return &tidbcloud.UpdateComponentVersionInCloudconfigResult{Stage: p.Stage}, nil
	}

	var components []string
	for name, c := range stageCfg.Components {
		if c.BaseImage == imageRepo {
			components = append(components, name)
		}
	}
	slices.Sort(components)

	componentVersion := normalizeComponentVersion(strings.SplitN(imageTag, "-", 2)[0])
	res := &tidbcloud.UpdateComponentVersionInCloudconfigResult{Stage: p.Stage}

	for _, component := range components {
		ticket, err := s.callOpsPlatformAPI(ctx, p.Stage, component, stageCfg.Components[component], imageRepo, imageTag, componentVersion)
		if err != nil {
			s.logger.Error().Err(err).Str("stage", p.Stage).Str("component", component).
				Str("image", p.Image).Str("image_tag", imageTag).Str("component_version", componentVersion).
				Msg("tidbcloud.update-component-version-in-cloudconfig failed")
			return nil, err
		}
		res.Tickets = append(res.Tickets, ticket)
	}

	s.logger.Info().Str("stage", p.Stage).Str("image", p.Image).Int("tickets", len(res.Tickets)).
		Msg("tidbcloud.update-component-version-in-cloudconfig succeeded")
	return res, nil
}

func (s *tidbcloudsrvc) callOpsPlatformAPI(
	ctx context.Context,
	stage string,
	component string,
	componentCfg OpsComponent,
	imageRepo, imageTag, componentVersion string,
) (*tidbcloud.TidbcloudOpsTicket, error) {
	opsCfg := s.opsConfig()
	stageCfg, ok := opsCfg.Stages[stage]
	if !ok || stageCfg.APIBaseURL == "" {
		return nil, fmt.Errorf("ops client is nil for stage %q: stage not found in config", stage)
	}

	var author, releaseID, changeID string
	if componentCfg.GitHubRepo != "" {
		// Tag metadata is optional: a missing lookup must not block the update.
		md, err := s.getTiBuildTagMetadata(ctx, componentCfg.GitHubRepo, imageTag)
		if err != nil {
			s.logger.Warn().Err(err).Str("stage", stage).Str("component", component).
				Str("github_repo", componentCfg.GitHubRepo).Str("image_tag", imageTag).
				Msg("failed to get tibuild tag metadata")
		} else if md != nil {
			author = md.Author
			releaseID = md.Meta.OpsReq.ReleaseID
			changeID = md.Meta.OpsReq.ChangeID
		}
	}

	payload := OpsUpdateComponentRequest{
		ClusterType: componentCfg.ClusterType,
		Version:     componentVersion,
		BaseImage:   imageRepo,
		Tag:         imageTag,
		Policy:      "immediate",
		Author:      author,
		ReleaseID:   releaseID,
		ChangeID:    changeID,
	}

	headers := map[string]string{}
	if stageCfg.APIKey != "" {
		headers["x-api-key"] = stageCfg.APIKey
	}

	status, body, err := s.postJSON(ctx, strings.TrimSuffix(stageCfg.APIBaseURL, "/")+updateComponentVersionAPIPath+url.PathEscape(component), headers, &payload)
	if err != nil {
		return nil, fmt.Errorf("update ops config for component %s: %w", component, err)
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("update ops config for component %s: http status %d: %s", component, status, strings.TrimSpace(string(body)))
	}

	var out OpsUpdateComponentResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("update ops config for component %s: decode response: %w", component, err)
	}
	if out.InstanceID <= 0 {
		return nil, fmt.Errorf("ops response missing instance_id for component %s", component)
	}

	var releaseIDPtr, changeIDPtr *string
	if releaseID != "" {
		releaseIDPtr = &releaseID
	}
	if changeID != "" {
		changeIDPtr = &changeID
	}

	return &tidbcloud.TidbcloudOpsTicket{
		ID:               fmt.Sprintf("%d", out.InstanceID),
		URL:              opsInstanceURL(stage, out.InstanceID),
		ReleaseID:        releaseIDPtr,
		ChangeID:         changeIDPtr,
		Component:        component,
		ComponentVersion: componentVersion,
	}, nil
}

// getTiBuildTagMetadata resolves the author and ops request ids attached to a
// tidbx git tag. An unconfigured API returns no metadata instead of failing.
func (s *tidbcloudsrvc) getTiBuildTagMetadata(ctx context.Context, githubRepo, imageTag string) (*TiBuildTagMetadataResponse, error) {
	opsCfg := s.opsConfig()
	if opsCfg == nil || opsCfg.TiBuildV2.APIBaseURL == "" {
		return nil, nil
	}

	endpoint := strings.TrimSuffix(opsCfg.TiBuildV2.APIBaseURL, "/") + "/hotfix/tidbx/tag?repo=" +
		url.QueryEscape(githubRepo) + "&tag=" + url.QueryEscape(imageTag)

	headers := map[string]string{}
	if opsCfg.TiBuildV2.User != "" && opsCfg.TiBuildV2.Password != "" {
		headers["Authorization"] = basicAuthHeader(opsCfg.TiBuildV2.User, opsCfg.TiBuildV2.Password)
	}

	var out TiBuildTagMetadataResponse
	if err := s.getJSON(ctx, endpoint, headers, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func opsInstanceURL(stage string, instanceID int) string {
	if stage == "prod" {
		return fmt.Sprintf("https://ops.tidbcloud.com/operations/%d", instanceID)
	}
	return fmt.Sprintf("https://ops-%s.tidbcloud.com/operations/%d", stage, instanceID)
}

func isSupportedNextgenImageTag(tag string) bool {
	if legacyNextgenImageTagRegexp.MatchString(tag) {
		return true
	}
	if calendarSemverNextgenImageTagRegexp.MatchString(tag) {
		baseTag := strings.TrimSuffix(tag, "-nextgen")
		return semver.IsValid(baseTag) &&
			!strings.Contains(strings.TrimPrefix(baseTag, "v"), "-") &&
			semver.Compare(baseTag, nextgenCalendarTagThreshold) >= 0
	}
	return false
}

func normalizeComponentVersion(version string) string {
	if v263VersionRegexp.MatchString(version) {
		return v263FixedVersion
	}
	return version
}
