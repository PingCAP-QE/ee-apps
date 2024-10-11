package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"slices"
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/rs/zerolog"
	"oras.land/oras-go/v2/registry/remote"
	"oras.land/oras-go/v2/registry/remote/auth"
	"oras.land/oras-go/v2/registry/remote/retry"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
)

type Handler struct {
	mirrorURL      string
	larkWebhookURL string
	logger         zerolog.Logger
}

func NewHandler(mirrorURL, larkWebhookURL string, logger *zerolog.Logger) (*Handler, error) {
	handler := Handler{mirrorURL: mirrorURL, larkWebhookURL: larkWebhookURL}
	if logger == nil {
		handler.logger = zerolog.New(os.Stderr).With().Timestamp().Logger()
	} else {
		handler.logger = *logger
	}

	return &handler, nil
}

func (h *Handler) SupportEventTypes() []string {
	return []string{EventTypeTiupPublishRequest}
}

// Handle for test case run events
func (h *Handler) Handle(event cloudevents.Event) cloudevents.Result {
	if !slices.Contains(h.SupportEventTypes(), event.Type()) {
		return cloudevents.ResultNACK
	}

	data := new(PublishRequestEvent)
	if err := event.DataAs(&data); err != nil {
		return cloudevents.NewReceipt(false, "invalid data: %v", err)
	}

	return h.handleImpl(data)
}

func (h *Handler) handleImpl(data *PublishRequestEvent) cloudevents.Result {
	// 1. get the the tarball from data.From.
	saveTo, err := downloadFile(data)
	if err != nil {
		h.logger.Err(err).Msg("download file failed")
		// h.notifyLark(&data.Publish, err)
		return cloudevents.NewReceipt(true, "download file failed: %v", err)
	}
	h.logger.Info().Msg("download file success")

	// 2. publish the tarball to the mirror.
	if err := h.publish(saveTo, &data.Publish); err != nil {
		h.logger.Err(err).Msg("publish to mirror failed")
		h.notifyLark(&data.Publish, err)
		return cloudevents.NewReceipt(true, "publish to mirror failed: %v", err)
	}
	h.logger.Info().Msg("publish to mirror success")

	// 3. check the package is in the mirror.
	//     printf 'post_check "$(tiup mirror show)/%s-%s-%s-%s.tar.gz" "%s"\n' \
	remoteURL := fmt.Sprintf("%s/%s-%s-%s-%s.tar.gz", h.mirrorURL, data.Publish.Name, data.Publish.Version, data.Publish.OS, data.Publish.Arch)
	if err := postCheck(saveTo, remoteURL); err != nil {
		h.logger.Err(err).Str("remote", remoteURL).Msg("post check failed")
		return cloudevents.NewReceipt(true, "post check failed: %v", err)
	}

	h.logger.Info().Str("remote", remoteURL).Msg("post check success")
	return cloudevents.ResultACK
}

func (h *Handler) notifyLark(publishInfo *PublishInfo, err error) {
	if h.larkWebhookURL == "" {
		return
	}

	message := fmt.Sprintf("Failed to publish %s-%s @%s/%s platform to mirror %s: %v",
		publishInfo.Name,
		publishInfo.Version,
		publishInfo.OS,
		publishInfo.Arch,
		h.mirrorURL,
		err)

	payload := map[string]interface{}{
		"msg_type": "text",
		"content": map[string]string{
			"text": message,
		},
	}

	jsonPayload, err := json.Marshal(payload)
	if err != nil {
		h.logger.Err(err).Msg("failed to marshal JSON payload")
	}

	resp, err := http.Post(h.larkWebhookURL, "application/json", bytes.NewBuffer(jsonPayload))
	if err != nil {
		h.logger.Err(err).Msg("failed to send notification to Lark")
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		h.logger.Error().Msgf("Lark API returned non-OK status: %d", resp.StatusCode)
	}
}

func downloadFile(data *PublishRequestEvent) (string, error) {
	switch data.From.Type {
	case FromTypeOci:
		// save to file with `saveTo` param:
		return downloadOCIFile(data.From.Oci)
	case FromTypeHTTP:
		return downloadHTTPFile(data.From.HTTP.URL)
	default:
		return "", fmt.Errorf("unknown from type: %v", data.From.Type)
	}
}

func (h *Handler) publish(file string, info *PublishInfo) error {
	args := []string{"mirror", "publish", info.Name, info.Version, file, info.EntryPoint, "--os", info.OS, "--arch", info.Arch, "--desc", info.Description}
	if info.Standalone {
		args = append(args, "--standalone")
	}
	command := exec.Command("tiup", args...)
	command.Env = os.Environ()
	command.Env = append(command.Env, "TIUP_MIRRORS="+h.mirrorURL)
	h.logger.Debug().Any("args", command.Args).Any("env", command.Args).Msg("will execute tiup command")
	output, err := command.Output()
	if err != nil {
		h.logger.Err(err).Msg("failed to execute tiup command")
		return err
	}
	h.logger.Info().
		Str("mirror", h.mirrorURL).
		Str("output", string(output)).
		Msg("tiup package publish success")

	return nil
}

// check the remote file after published.
func postCheck(localFile, remoteFileURL string) error {
	// 1. Calculate the sha256sum of the local file
	localSum, err := calculateSHA256(localFile)
	if err != nil {
		return fmt.Errorf("failed to calculate local file sha256: %v", err)
	}

	// 2. Download the remote file
	tempFile, err := downloadHTTPFile(remoteFileURL)
	if err != nil {
		return fmt.Errorf("failed to download remote file: %v", err)
	}
	defer os.Remove(tempFile)

	// 3. Calculate the sha256sum of the remote file
	remoteSum, err := calculateSHA256(tempFile)
	if err != nil {
		return fmt.Errorf("failed to calculate remote file sha256: %v", err)
	}

	// 4. Compare the two sha256sums
	if localSum != remoteSum {
		return fmt.Errorf("sha256 mismatch: local %s, remote %s", localSum, remoteSum)
	}

	return nil
}

func calculateSHA256(filePath string) (string, error) {
	f, err := os.Open(filePath)
	if err != nil {
		return "", err
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}

	return fmt.Sprintf("%x", h.Sum(nil)), nil
}

func downloadHTTPFile(url string) (string, error) {
	resp, err := http.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	tempFile, err := os.CreateTemp("", "remote_file_*")
	if err != nil {
		return "", err
	}
	defer tempFile.Close()

	_, err = io.Copy(tempFile, resp.Body)
	if err != nil {
		return "", err
	}

	return tempFile.Name(), nil
}

func downloadOCIFile(from *FromOci) (string, error) {
	repo, err := getOciRepo(from.Repo)
	if err != nil {
		return "", err
	}
	rc, _, err := oci.NewFileReadCloser(context.Background(), repo, from.Tag, from.File)
	if err != nil {
		return "", err
	}
	defer rc.Close()

	tempFile, err := os.CreateTemp("", "oci_download_*.tar.gz")
	if err != nil {
		return "", err
	}
	defer tempFile.Close()

	if _, err := io.Copy(tempFile, rc); err != nil {
		return "", err
	}

	return tempFile.Name(), nil
}

func getOciRepo(repo string) (*remote.Repository, error) {
	repository, err := remote.NewRepository(repo)
	if err != nil {
		return nil, err
	}

	reg := strings.SplitN(repo, "/", 2)[0]
	repository.Client = &auth.Client{
		Client:     retry.DefaultClient,
		Cache:      auth.DefaultCache,
		Credential: auth.StaticCredential(reg, auth.EmptyCredential),
	}

	return repository, nil
}
