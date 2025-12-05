package tiup

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
	redsync "github.com/go-redsync/redsync/v4"
	goredis "github.com/go-redsync/redsync/v4/redis/goredis/v8"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
)

const (
	tiupPublishingMutexName       = "global/mutex/tiup-publishing"
	tiupPublishingMutexExpiry     = 10 * time.Minute
	tiupPublishingMutexTries      = 300
	tiupPublishingMutexRetryDelay = time.Second

	tiupUploadingMaxRetries = 3
	tiupUploadingRetryDelay = time.Minute
)

type tiupWorker struct {
	logger      zerolog.Logger
	redisClient redis.UniversalClient
	mutex       *redsync.Mutex
	options     struct {
		LarkWebhookURL   string
		MirrorName       string
		MirrorURL        string
		PublicServiceURL string
		NightlyInterval  time.Duration
	}
}

func NewWorker(logger *zerolog.Logger, redisClient redis.UniversalClient, options map[string]string) (impl.Worker, error) {
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
	handler.options.MirrorName = options["mirror_name"]
	handler.options.MirrorURL = options["mirror_url"]
	handler.options.LarkWebhookURL = options["lark_webhook_url"]
	if options["public_service_url"] != "" {
		handler.options.PublicServiceURL = options["public_service_url"]
	} else {
		handler.options.PublicServiceURL = "http://publisher-<env>-mirror.<namespace>.svc"
	}

	pool := goredis.NewPool(redisClient)
	rs := redsync.New(pool)

	// Obtain a new mutex by using the same name for all instances wanting the
	// same lock.
	handler.mutex = rs.NewMutex(tiupPublishingMutexName,
		redsync.WithExpiry(tiupPublishingMutexExpiry),
		redsync.WithTries(tiupPublishingMutexTries),
		redsync.WithRetryDelay(tiupPublishingMutexRetryDelay),
	)

	return &handler, nil
}

func (p *tiupWorker) SupportEventTypes() []string {
	return []string{EventTypeTiupPublishRequest}
}

// Handle for tiup publication request events
func (p *tiupWorker) Handle(event cloudevents.Event) cloudevents.Result {
	if !slices.Contains(p.SupportEventTypes(), event.Type()) {
		return cloudevents.ResultNACK
	}
	// Skip the messages that are not for the current mirror
	if event.Subject() != p.options.MirrorName {
		return cloudevents.ResultNACK
	}

	p.redisClient.SetXX(context.Background(), event.ID(), share.PublishStateProcessing, redis.KeepTTL)

	data := new(PublishRequestTiUP)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	result := p.rateLimit(data, p.options.NightlyInterval, p.handle)
	switch {
	case cloudevents.IsACK(result):
		p.redisClient.SetXX(context.Background(), event.ID(), share.PublishStateSuccess, redis.KeepTTL)
	case cloudevents.IsNACK(result):
		p.redisClient.SetXX(context.Background(), event.ID(), share.PublishStateFailed, redis.KeepTTL)
		p.notifyLark(data, result)
	default:
		p.redisClient.SetXX(context.Background(), event.ID(), share.PublishStateCanceled, redis.KeepTTL)
	}

	return result
}

func (p *tiupWorker) rateLimit(data *PublishRequestTiUP, ttl time.Duration, run func(*PublishRequestTiUP) cloudevents.Result) cloudevents.Result {
	//  Skip rate limiting for no nightly builds
	if !isNightlyTiup(data.Publish) || ttl <= 0 {
		return run(data)
	}

	// Add rate limiting
	rateLimitKey := fmt.Sprintf("%s:%s:%s:%s:%s", redisKeyPrefixTiupRateLimit,
		p.options.MirrorURL, data.Publish.Name, data.Publish.OS, data.Publish.Arch)
	count, err := p.redisClient.Incr(context.Background(), rateLimitKey).Result()
	if err != nil {
		p.logger.Err(err).Msg("rate limit check failed")
		return cloudevents.NewReceipt(false, "rate limit check failed: %v", err)
	}

	// First request sets expiry
	if count <= 1 {
		p.redisClient.Expire(context.Background(), rateLimitKey, ttl)
		return run(data)
	}
	p.logger.Debug().Str("key", rateLimitKey).Msg("cache key")

	p.logger.Warn().
		Str("mirror", p.options.MirrorName).
		Str("pkg", data.Publish.Name).
		Str("os", data.Publish.OS).
		Str("arch", data.Publish.Arch).
		Dur("ttl", ttl).
		Int64("count", count).
		Msg("rate limit execeeded for package")

	return fmt.Errorf("skip: rate limit exceeded for package %s", data.Publish.Name)
}

func (p *tiupWorker) handle(data *PublishRequestTiUP) cloudevents.Result {
	// 1. get the the tarball from data.From.
	saveTo, err := share.DownloadFile(&data.From)
	if err != nil {
		p.logger.Err(err).Msg("download file failed")
		return cloudevents.NewReceipt(false, "download file failed: %v", err)
	}
	p.logger.Info().Msg("download file success")

	// 2. publish the tarball to the mirror with retries.
	for i := range tiupUploadingMaxRetries {
		if err = p.publish(saveTo, &data.Publish); err == nil {
			break
		}

		if i < tiupUploadingMaxRetries-1 {
			p.logger.Warn().
				Int("tried", i+1).
				Int("max_retries", tiupUploadingMaxRetries).
				Err(err).
				Msgf("publish to mirror failed, i will retry after %s.", tiupUploadingRetryDelay)
			time.Sleep(tiupUploadingRetryDelay)
		}
	}

	if err != nil {
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

func (p *tiupWorker) notifyLark(req *PublishRequestTiUP, err error) {
	if p.options.LarkWebhookURL == "" {
		return
	}

	info := share.FailureNotifyInfo{
		Title:         "TiUP Publish Failed",
		FailedMessage: err.Error(),
		Params: [][2]string{
			{"package", req.Publish.Name},
			{"version", req.Publish.Version},
			{"os", req.Publish.OS},
			{"arch", req.Publish.Arch},
			{"to-mirror", p.options.MirrorName},
			{"from", req.From.String()},
		},
	}

	jsonPayload, err := share.NewLarkCardWithGoTemplate(info)
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

func (p *tiupWorker) publish(file string, info *PublishInfoTiUP) error {
	// Obtain a lock for our given global TiUP mirrors mutex.
	// After this is successful, no one else can obtain the same
	// lock (the same mutex name) until we unlock it.
	if err := p.mutex.Lock(); err != nil {
		return fmt.Errorf("failed to obtain lock: %v", err)
	}
	defer p.mutex.Unlock()

	args := []string{"mirror", "publish", info.Name, info.Version, file, info.EntryPoint, "--os", info.OS, "--arch", info.Arch, "--desc", info.Description}
	if info.Standalone {
		args = append(args, "--standalone")
	}
	command := exec.Command("tiup", args...)
	command.Env = os.Environ()
	command.Env = append(command.Env, "TIUP_MIRRORS="+p.options.MirrorURL)
	p.logger.Debug().Any("args", command.Args).Any("env", command.Args).Msg("will execute tiup command")
	output, err := command.CombinedOutput()
	if err != nil {
		p.logger.Err(err).Bytes("output", output).Msg("tiup command execute failed")
		return fmt.Errorf("tiup command execute failed:\n%s", output)
	}

	p.logger.Info().
		Str("mirror", p.options.MirrorURL).
		Str("output", string(output)).
		Msg("tiup package publish success")

	return nil
}
