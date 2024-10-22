package tiup

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"slices"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog"
)

type Publisher struct {
	mirrorURL      string
	larkWebhookURL string
	logger         zerolog.Logger
	redisClient    redis.Cmdable
}

func NewPublisher(mirrorURL, larkWebhookURL string, logger *zerolog.Logger, redisClient redis.Cmdable) (*Publisher, error) {
	handler := Publisher{mirrorURL: mirrorURL, larkWebhookURL: larkWebhookURL, redisClient: redisClient}
	if logger == nil {
		handler.logger = zerolog.New(os.Stderr).With().Timestamp().Logger()
	} else {
		handler.logger = *logger
	}

	return &handler, nil
}

func (p *Publisher) SupportEventTypes() []string {
	return []string{EventTypeTiupPublishRequest}
}

// Handle for test case run events
func (p *Publisher) Handle(event cloudevents.Event) cloudevents.Result {
	if !slices.Contains(p.SupportEventTypes(), event.Type()) {
		return cloudevents.ResultNACK
	}
	p.redisClient.SetXX(context.Background(), event.ID(), PublishStateProcessing, redis.KeepTTL)

	data := new(PublishRequest)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	result := p.handleImpl(data)
	switch {
	case cloudevents.IsACK(result):
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateSuccess, redis.KeepTTL)
	default:
		p.redisClient.SetXX(context.Background(), event.ID(), PublishStateFailed, redis.KeepTTL)
		p.notifyLark(&data.Publish, result)
	}

	return result
}

func (p *Publisher) handleImpl(data *PublishRequest) cloudevents.Result {
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
	remoteURL := fmt.Sprintf("%s/%s-%s-%s-%s.tar.gz", p.mirrorURL, data.Publish.Name, data.Publish.Version, data.Publish.OS, data.Publish.Arch)
	if err := postCheck(saveTo, remoteURL); err != nil {
		p.logger.Err(err).Str("remote", remoteURL).Msg("post check failed")
		return cloudevents.NewReceipt(false, "post check failed: %v", err)
	}

	p.logger.Info().Str("remote", remoteURL).Msg("post check success")
	return cloudevents.ResultACK
}

func (p *Publisher) notifyLark(publishInfo *PublishInfo, err error) {
	if p.larkWebhookURL == "" {
		return
	}

	message := fmt.Sprintf("Failed to publish %s-%s @%s/%s platform to mirror %s: %v",
		publishInfo.Name,
		publishInfo.Version,
		publishInfo.OS,
		publishInfo.Arch,
		p.mirrorURL,
		err)

	payload := map[string]interface{}{
		"msg_type": "text",
		"content": map[string]string{
			"text": message,
		},
	}

	jsonPayload, err := json.Marshal(payload)
	if err != nil {
		p.logger.Err(err).Msg("failed to marshal JSON payload")
	}

	resp, err := http.Post(p.larkWebhookURL, "application/json", bytes.NewBuffer(jsonPayload))
	if err != nil {
		p.logger.Err(err).Msg("failed to send notification to Lark")
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		p.logger.Error().Msgf("Lark API returned non-OK status: %d", resp.StatusCode)
	}
}

func (p *Publisher) publish(file string, info *PublishInfo) error {
	args := []string{"mirror", "publish", info.Name, info.Version, file, info.EntryPoint, "--os", info.OS, "--arch", info.Arch, "--desc", info.Description}
	if info.Standalone {
		args = append(args, "--standalone")
	}
	command := exec.Command("tiup", args...)
	command.Env = os.Environ()
	command.Env = append(command.Env, "TIUP_MIRRORS="+p.mirrorURL)
	p.logger.Debug().Any("args", command.Args).Any("env", command.Args).Msg("will execute tiup command")
	output, err := command.Output()
	if err != nil {
		p.logger.Err(err).Msg("failed to execute tiup command")
		return err
	}
	p.logger.Info().
		Str("mirror", p.mirrorURL).
		Str("output", string(output)).
		Msg("tiup package publish success")

	return nil
}
