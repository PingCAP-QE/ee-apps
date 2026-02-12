package tidbcloud

import (
	"fmt"
	"strings"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/crane"
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

// getCraneOptions returns crane.Option based on the image auth configuration.
func (s *tidbcloudsrvc) getCraneOptions() []crane.Option {
	if s.tpsCfg == nil {
		return nil
	}

	authCfg := s.tpsCfg.ImageAuth

	// If use_default_keychain is true, let crane use the default keychain
	if authCfg.UseDefaultKeychain {
		return nil
	}

	// If username and password are provided, use them
	if authCfg.Username != "" && authCfg.Password != "" {
		auth := authn.FromConfig(authn.AuthConfig{
			Username: authCfg.Username,
			Password: authCfg.Password,
		})
		return []crane.Option{crane.WithAuth(auth)}
	}

	return nil
}
