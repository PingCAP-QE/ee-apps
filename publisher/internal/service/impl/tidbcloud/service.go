package tidbcloud

import (
	"context"
	"fmt"
	"strings"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

// tidbcloud service example implementation.
// The example methods log the requests and return zero values.
type tidbcloudsrvc struct {
	*share.BaseService
	opsCfg *OpsConfig
	tpsCfg *TestPlatformsConfig
	// kernelImageMetaReader overrides the default source image metadata reader (used in tests).
	kernelImageMetaReader func(ctx context.Context, image string) kernelImageMeta
}

// NewService returns the tidbcloud service implementation.
func NewService(logger *zerolog.Logger, cfg config.Service) tidbcloud.Service {
	srvc := &tidbcloudsrvc{BaseService: share.NewBaseServiceService(logger, cfg)}

	tidbcloudCfg := cfg.Services["tidbcloud"]
	switch v := tidbcloudCfg.(type) {
	case map[string]any:
		// load ops config
		if configFileAny, ok := v["ops_config_file"]; ok {
			configFile, ok := configFileAny.(string)
			if !ok || strings.TrimSpace(configFile) == "" {
				srvc.Logger.Fatal().Msg("tidbcloud.ops_config_file must be a non-empty string")
			}
			ret, err := config.Load[OpsConfig](configFile)
			if err != nil {
				srvc.Logger.Fatal().Err(err).Msg("failed to load tidbcloud ops config")
			}
			srvc.opsCfg = ret
		}

		// load test platform config
		if configFileAny, ok := v["testplatforms_config_file"]; ok {
			configFile, ok := configFileAny.(string)
			if !ok || strings.TrimSpace(configFile) == "" {
				srvc.Logger.Fatal().Msg("tidbcloud.testplatforms_config_file must be a non-empty string")
			}
			ret, err := config.Load[TestPlatformsConfig](configFile)
			if err != nil {
				srvc.Logger.Fatal().Err(err).Msg("failed to load test platforms config")
			}
			srvc.tpsCfg = ret
		}
	}

	return srvc
}

// Reload re-creates Kafka/Redis clients and reloads ops and test platforms configs.
func (s *tidbcloudsrvc) Reload(cfg config.Service) {
	s.BaseService.Reload(cfg)

	tidbcloudCfg := cfg.Services["tidbcloud"]
	switch v := tidbcloudCfg.(type) {
	case map[string]any:
		if configFileAny, ok := v["ops_config_file"]; ok {
			configFile, ok := configFileAny.(string)
			if ok && strings.TrimSpace(configFile) != "" {
				ret, err := config.Load[OpsConfig](configFile)
				if err != nil {
					s.Logger.Err(err).Msg("failed to reload ops config")
				} else {
					s.opsCfg = ret
					s.Logger.Info().Msg("ops config reloaded")
				}
			}
		}
		if configFileAny, ok := v["testplatforms_config_file"]; ok {
			configFile, ok := configFileAny.(string)
			if ok && strings.TrimSpace(configFile) != "" {
				ret, err := config.Load[TestPlatformsConfig](configFile)
				if err != nil {
					s.Logger.Err(err).Msg("failed to reload test platforms config")
				} else {
					s.tpsCfg = ret
					s.Logger.Info().Msg("test platforms config reloaded")
				}
			}
		}
	}
}

func parseImageRepoTag(image string) (string, string, error) {
	// use existing helper for "@sha256:" support
	if strings.Contains(image, "@sha256:") {
		return share.SplitRepoAndTag(image)
	}
	// split by last ':' to support registry with port
	idx := strings.LastIndex(image, ":")
	if idx < 0 || idx == len(image)-1 {
		return "", "", fmt.Errorf("invalid image: %s", image)
	}
	return image[:idx], image[idx+1:], nil
}
