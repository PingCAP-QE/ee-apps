package impl

import (
	"net/http"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"

	"github.com/bndr/gojenkins"
)

// Reload updates the service configuration at runtime. It is called by the
// config reloader when the config file changes on disk.
func (s *devbuildsrvc) Reload(cfg *config.Service) {
	s.logger.Info().Msg("hot-reloading devbuild configuration")

	// Update GitHub client with new token.
	s.ghClient = NewGitHubClientWithConfig(cfg.Github)

	// Update the request policy as one immutable snapshot. A request must never
	// combine the new allowlist with the old maps, so the whole snapshot is
	// replaced at once. The watcher validates the revision before calling
	// Reload, so a bad list here is a construction bug: keep the previous
	// snapshot rather than widening the policy to "allow everything".
	if policy, err := newDevbuildPolicy(cfg); err != nil {
		s.logger.Err(err).Msg("invalid devbuild github_repo_allowlist on reload, keeping the previous policy")
	} else {
		s.policy.Store(policy)
	}

	// Update Tekton dashboard URLs used for server-side API calls and user-facing links.
	s.dashboardAPIURL = cfg.Tekton.APIBaseURL()
	s.dashboardViewURL = cfg.Tekton.ViewBaseURL()
	s.ociFileDownloadURL = strings.TrimRight(cfg.Tekton.OciFileDownloadURL, "/")
	logTektonURLs(s.logger, cfg)

	// Update reconciler lookback duration.
	if cfg.Tekton.ReconcilerSince != "" {
		if d, err := time.ParseDuration(cfg.Tekton.ReconcilerSince); err == nil {
			s.reconcilerSince = d
		} else {
			s.logger.Warn().Str("reconciler_since", cfg.Tekton.ReconcilerSince).Msg("invalid reconciler_since in reload, keeping previous value")
		}
	}

	// Recreate cloud events client if endpoint changed. This also re-reads the
	// HMAC secret, so rotating it in the mounted config takes effect without a
	// restart.
	if cfg.Tekton.CloudeventEndpoint != "" {
		client, err := newTektonCloudEventClient(cfg.Tekton.CloudeventEndpoint, cfg.Tekton.CloudeventHmacSecret, s.logger)
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
			s.larkNotifier.Reload(cfg.Lark.AppID, cfg.Lark.AppSecret, cfg.Lark.Channels)
			s.larkNotifier.configureArtifacts(cfg)
			s.logger.Debug().Msg("lark notifier credentials and channels updated on reload")
		} else if !cfg.Lark.Enabled {
			s.larkNotifier.Disable()
			s.logger.Debug().Msg("lark notifier disabled on reload")
		}
	}

	s.logger.Info().Msg("devbuild configuration reloaded successfully")
}
