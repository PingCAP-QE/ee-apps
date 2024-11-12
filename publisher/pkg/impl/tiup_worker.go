package impl

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"slices"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog"
)

type tiupWorker struct {
	logger      zerolog.Logger
	redisClient redis.Cmdable
	options     struct {
		LarkWebhookURL   string
		MirrorURL        string
		PublicServiceURL string
		NightlyInterval  time.Duration
	}
}

func NewTiupWorker(logger *zerolog.Logger, redisClient redis.Cmdable, options map[string]string) (*tiupWorker, error) {
	handler := tiupWorker{redisClient: redisClient}
	if logger == nil {
		handler.logger = zerolog.New(os.Stderr).With().Timestamp().Logger()
	} else {
		handler.logger = *logger
	}

	nigthlyInterval, err := time.ParseDuration(options["nightly_interval"])
	if err != nil {
		return nil, fmt.Errorf("parsing nightly interval failed: %v", err)
	}
	handler.options.NightlyInterval = nigthlyInterval
	handler.options.MirrorURL = options["mirror_url"]
	handler.options.LarkWebhookURL = options["lark_webhook_url"]
	if options["public_service_url"] != "" {
		handler.options.PublicServiceURL = options["public_service_url"]
	} else {
		handler.options.PublicServiceURL = "http://publisher-<env>-mirror.<namespace>.svc"
	}

	return &handler, nil
}

func (p *tiupWorker) SupportEventTypes() []string {
	return []string{EventTypeTiupPublishRequest}
}

// Handle for test case run events
func (p *tiupWorker) Handle(event cloudevents.Event) cloudevents.Result {
	if !slices.Contains(p.SupportEventTypes(), event.Type()) {
		return cloudevents.ResultNACK
	}
	p.redisClient.SetXX(context.Background(), event.ID(), PublishStateProcessing, redis.KeepTTL)

	data := new(PublishRequest)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	result := p.rateLimit(data, p.options.NightlyInterval, p.handle)
	switch {
	case cloudevents.IsACK(result):
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateSuccess, redis.KeepTTL)
	case cloudevents.IsNACK(result):
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateFailed, redis.KeepTTL)
		p.notifyLark(data, result)
	default:
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateCanceled, redis.KeepTTL)
	}

	return result
}

func (p *tiupWorker) rateLimit(data *PublishRequest, ttl time.Duration, run func(*PublishRequest) cloudevents.Result) cloudevents.Result {
	//  Skip rate limiting for no nightly builds
	if !isNightlyTiup(data.Publish) || ttl <= 0 {
		return run(data)
	}

	// Add rate limiting
	rateLimitKey := fmt.Sprintf("ratelimit:tiup:%s:%s:%s:%s", p.options.MirrorURL, data.Publish.Name, data.Publish.OS, data.Publish.Arch)
	count, err := p.redisClient.Incr(context.Background(), rateLimitKey).Result()
	if err != nil {
		p.logger.Err(err).Msg("rate limit check failed")
		return cloudevents.NewReceipt(false, "rate limit check failed: %v", err)
	}

	// First request sets expiry
	if count == 1 {
		p.redisClient.Expire(context.Background(), rateLimitKey, ttl)
		return run(data)
	}
	p.logger.Debug().Str("key", rateLimitKey).Msg("cache key")

	p.logger.Warn().
		Str("mirror", p.options.MirrorURL).
		Str("pkg", data.Publish.Name).
		Str("os", data.Publish.OS).
		Str("arch", data.Publish.Arch).
		Dur("ttl", ttl).
		Int64("count", count).
		Msg("rate limit execeeded for package")

	return fmt.Errorf("skip: rate limit exceeded for package %s", data.Publish.Name)
}

func (p *tiupWorker) handle(data *PublishRequest) cloudevents.Result {
	// 1. get the the tarball from data.From.
	saveTo, err := downloadFile(data)
	if err != nil {
		p.logger.Err(err).Msg("download file failed")
		return cloudevents.NewReceipt(false, "download file failed: %v", err)
	}
	p.logger.Info().Msg("download file success")

	// 2. publish the tarball to the mirror.
	if err := p.publish(saveTo, &data.Publish); err != nil {
		p.logger.Err(err).Msg("publish to mirror failed")
		return cloudevents.NewReceipt(false, "publish to mirror failed: %v", err)
	}
	p.logger.Info().Msg("publish to mirror success")

	// 3. check the package is in the mirror.
	//     printf 'post_check "$(tiup mirror show)/%s-%s-%s-%s.tar.gz" "%s"\n' \
	remoteURL := fmt.Sprintf("%s/%s-%s-%s-%s.tar.gz", p.options.MirrorURL, data.Publish.Name, data.Publish.Version, data.Publish.OS, data.Publish.Arch)
	if err := postCheckTiupPkg(saveTo, remoteURL); err != nil {
		p.logger.Err(err).Str("remote", remoteURL).Msg("post check failed")
		return cloudevents.NewReceipt(false, "post check failed: %v", err)
	}

	p.logger.Info().Str("remote", remoteURL).Msg("post check success")
	return cloudevents.ResultACK
}

func (p *tiupWorker) notifyLark(req *PublishRequest, err error) {
	if p.options.LarkWebhookURL == "" {
		return
	}

	rerunCmd := fmt.Sprintf(`go run %s --url %s tiup request-to-publish --body '{"artifact_url": "%s"}'`,
		"github.com/PingCAP-QE/ee-apps/publisher/cmd/publisher-cli@main",
		p.options.PublicServiceURL,
		req.From.String(),
	)

	info := failureNotifyInfo{
		Title:         "TiUP Publish Failed",
		FailedMessage: err.Error(),
		RerunCommands: rerunCmd,
		Params: [][2]string{
			{"package", req.Publish.Name},
			{"version", req.Publish.Version},
			{"os", req.Publish.OS},
			{"arch", req.Publish.Arch},
			{"to-mirror", p.options.MirrorURL},
			{"from", req.From.String()},
		},
	}

	jsonPayload, err := newLarkCardWithGoTemplate(info)
	if err != nil {
		p.logger.Err(err).Msg("failed to gen message payload")
		return
	}

	resp, err := http.Post(p.options.LarkWebhookURL, "application/json", bytes.NewBufferString(jsonPayload))
	if err != nil {
		p.logger.Err(err).Msg("failed to send notification to Lark")
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		p.logger.Error().Msgf("Lark API returned non-OK status: %d", resp.StatusCode)
	}
}

func (p *tiupWorker) publish(file string, info *PublishInfo) error {
	args := []string{"mirror", "publish", info.Name, info.Version, file, info.EntryPoint, "--os", info.OS, "--arch", info.Arch, "--desc", info.Description}
	if info.Standalone {
		args = append(args, "--standalone")
	}
	command := exec.Command("tiup", args...)
	command.Env = os.Environ()
	command.Env = append(command.Env, "TIUP_MIRRORS="+p.options.MirrorURL)
	p.logger.Debug().Any("args", command.Args).Any("env", command.Args).Msg("will execute tiup command")
	output, err := command.Output()
	if err != nil {
		p.logger.Err(err).Msg("failed to execute tiup command")
		return err
	}
	p.logger.Info().
		Str("mirror", p.options.MirrorURL).
		Str("output", string(output)).
		Msg("tiup package publish success")

	return nil
}
