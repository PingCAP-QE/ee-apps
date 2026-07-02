package impl

import (
	"net/http"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"

	"github.com/bndr/gojenkins"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/go-github/v69/github"
)

// Reload updates the service configuration at runtime. It is called by the
// config reloader when the config file changes on disk.
func (s *devbuildsrvc) Reload(cfg *config.Service) {
	s.logger.Info().Msg("hot-reloading devbuild configuration")

	// Update GitHub client with new token.
	s.ghClient = github.NewClientWithEnvProxy().WithAuthToken(cfg.Github.Token)

	// Update product repo and image mirror maps.
	s.productRepoMap = cfg.ProductRepoMap
	s.imageMirrorURLMap = cfg.ImageMirrorURLMap

	// Update Tekton dashboard URL.
	s.dashboardURL = cfg.Tekton.ViewURL

	// Update reconciler lookback duration.
	if cfg.Tekton.ReconcilerSince != "" {
		if d, err := time.ParseDuration(cfg.Tekton.ReconcilerSince); err == nil {
			s.reconcilerSince = d
		} else {
			s.logger.Warn().Str("reconciler_since", cfg.Tekton.ReconcilerSince).Msg("invalid reconciler_since in reload, keeping previous value")
		}
	}

	// Recreate cloud events client if endpoint changed.
	if cfg.Tekton.CloudeventEndpoint != "" {
		client, err := cloudevents.NewClientHTTP(cloudevents.WithTarget(cfg.Tekton.CloudeventEndpoint))
		if err == nil {
			s.tektonCloudEventClient = client
		} else {
			s.logger.Err(err).Msg("failed to recreate cloud events client on reload, keeping previous")
		}
	}

	// Update Jenkins client and job name.
	s.jenkins.client = gojenkins.CreateJenkins(http.DefaultClient, cfg.Jenkins.URL)
	s.jenkins.jobName = cfg.Jenkins.JobName

	// Update Lark notifier client if a notifier is registered.
	if s.larkNotifier != nil {
		if cfg.Lark.Enabled && cfg.Lark.AppID != "" && cfg.Lark.AppSecret != "" {
			s.larkNotifier.Reload(cfg.Lark.AppID, cfg.Lark.AppSecret)
			s.logger.Debug().Msg("lark notifier credentials updated on reload")
		} else if !cfg.Lark.Enabled {
			s.larkNotifier.Disable()
			s.logger.Debug().Msg("lark notifier disabled on reload")
		}
	}

	s.logger.Info().Msg("devbuild configuration reloaded successfully")
}
