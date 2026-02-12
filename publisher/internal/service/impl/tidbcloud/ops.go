package tidbcloud

import (
	"context"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
	"github.com/go-resty/resty/v2"
)

// UpdateComponentVersionInCloudconfig implements
// update-component-version-in-cloudconfig.
func (s *tidbcloudsrvc) UpdateComponentVersionInCloudconfig(ctx context.Context, p *tidbcloud.UpdateComponentVersionInCloudconfigPayload) (res *tidbcloud.UpdateComponentVersionInCloudconfigResult, err error) {
	if p == nil {
		return nil, fmt.Errorf("payload is nil")
	}
	res = &tidbcloud.UpdateComponentVersionInCloudconfigResult{Stage: p.Stage}
	if strings.TrimSpace(p.Stage) == "" {
		return nil, fmt.Errorf("stage is empty")
	}
	if strings.TrimSpace(p.Image) == "" {
		return nil, fmt.Errorf("image is empty")
	}
	if s.opsCfg == nil {
		return nil, fmt.Errorf("tidbcloud ops config is not configured")
	}
	stageCfg, ok := s.opsCfg.Stages[p.Stage]
	if !ok {
		return nil, fmt.Errorf("stage %q not found in tidbcloud ops config", p.Stage)
	}

	imageRepo, imageTag, err := parseImageRepoTag(p.Image)
	if err != nil {
		return nil, err
	}

	// Fast return if imageTag is not in format: vX.Y.Z-nextgen.YYDDMM.N
	// Example: v7.5.0-nextgen.240101.1
	re := regexp.MustCompile(`^v\d+\.\d+\.\d+-nextgen\.\d{6}\.\d+$`)
	if !re.MatchString(imageTag) {
		s.Logger.Info().Str("stage", p.Stage).Str("image", p.Image).Str("image_tag", imageTag).Msg("skip update: image_tag is not in expected format vX.Y.Z-nextgen.YYDDMM.N")
		return res, nil
	}

	var components []string
	for name, c := range stageCfg.Components {
		if c.BaseImage == imageRepo {
			components = append(components, name)
		}
	}
	slices.Sort(components)

	componentVersion := strings.SplitN(imageTag, "-", 2)[0]

	for _, component := range components {
		c := stageCfg.Components[component]
		ticket, err := s.callOpsPlatformAPI(ctx, p.Stage, component, c, imageRepo, imageTag, componentVersion)
		if err != nil {
			return nil, err
		}
		res.Tickets = append(res.Tickets, ticket)
	}

	s.Logger.Info().Str("stage", p.Stage).Str("image", p.Image).Int("tickets", len(res.Tickets)).Msg("tidbcloud.update-component-version-in-cloudconfig")
	return res, nil
}

func (s *tidbcloudsrvc) callOpsPlatformAPI(ctx context.Context, stage string, component string, componentCfg OpsComponent, imageRepo, imageTag, componentVersion string) (*tidbcloud.TidbcloudOpsTicket, error) {
	var author, releaseID, changeID string
	if componentCfg.GitHubRepo != "" {
		md, mdErr := s.getTiBuildTagMetadata(ctx, componentCfg.GitHubRepo, imageTag)
		if mdErr != nil {
			s.Logger.Warn().Err(mdErr).Str("stage", stage).Str("component", component).Msg("failed to get tibuild tag metadata")
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

	var out OpsUpdateComponentResponse
	resp, err := s.opsRestyClient(stage).R().
		SetContext(ctx).
		SetBody(&payload).
		SetResult(&out).
		SetPathParam("component", component).
		Post("/{component}")
	if err != nil {
		return nil, fmt.Errorf("update ops config for component %s: %w", component, err)
	}
	if resp.IsError() {
		return nil, fmt.Errorf("update ops config for component %s: http status %d: %s", component, resp.StatusCode(), strings.TrimSpace(string(resp.Body())))
	}
	if out.InstanceID <= 0 {
		return nil, fmt.Errorf("ops response missing instance_id for component %s", component)
	}

	ticketURL := opsInstanceURL(stage, out.InstanceID)
	var releaseIDPtr, changeIDPtr *string
	if releaseID != "" {
		releaseIDPtr = &releaseID
	}
	if changeID != "" {
		changeIDPtr = &changeID
	}

	return &tidbcloud.TidbcloudOpsTicket{
		ID:               fmt.Sprintf("%d", out.InstanceID),
		URL:              ticketURL,
		ReleaseID:        releaseIDPtr,
		ChangeID:         changeIDPtr,
		Component:        component,
		ComponentVersion: componentVersion,
	}, nil
}

func (s *tidbcloudsrvc) tibuildRestyClient() *resty.Client {
	cfg := s.opsCfg.TiBuildV2
	client := resty.New().SetBaseURL(cfg.APIBaseURL)

	if cfg.User != "" && cfg.Password != "" {
		client.SetBasicAuth(cfg.User, cfg.Password)
	}
	return client
}

func (s *tidbcloudsrvc) opsRestyClient(stage string) *resty.Client {
	cfg, ok := s.opsCfg.Stages[stage]
	if !ok {
		return nil
	}

	return resty.New().
		SetBaseURL(cfg.APIBaseURL).
		SetHeader("x-api-key", cfg.APIKey)
}

func (s *tidbcloudsrvc) getTiBuildTagMetadata(ctx context.Context, githubRepo, imageTag string) (*TiBuildTagMetadataResponse, error) {
	var out TiBuildTagMetadataResponse
	req := s.tibuildRestyClient().R().
		SetContext(ctx).
		SetHeader("Accept", "application/json").
		SetResult(&out).
		SetQueryParam("repo", githubRepo).
		SetQueryParam("tag", imageTag)
	resp, err := req.Get("/hotfix/tidbx/tag")
	if err != nil {
		return nil, err
	}
	if resp.IsError() {
		return nil, fmt.Errorf("tibuild-v2 http status %d: %s", resp.StatusCode(), strings.TrimSpace(string(resp.Body())))
	}
	return &out, nil
}

func opsInstanceURL(stage string, instanceID int) string {
	if stage == "prod" {
		return fmt.Sprintf("https://ops.tidbcloud.com/operations/%d", instanceID)
	}
	return fmt.Sprintf("https://ops-%s.tidbcloud.com/operations/%d", stage, instanceID)
}
