package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"slices"
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/rs/zerolog/log"
	"oras.land/oras-go/v2/registry/remote"
	"oras.land/oras-go/v2/registry/remote/auth"
	"oras.land/oras-go/v2/registry/remote/retry"

	"github.com/PingCAP-QE/ee-apps/dl/pkg/oci"
)

type Handler struct {
	mirrorsURL string
}

func NewHandler(mirror string) (*Handler, error) {
	return &Handler{mirror}, nil
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
	saveTo, err := h.downloadFile(data)
	if err != nil {
		return cloudevents.NewReceipt(true, "download file failed: %v", err)
	}

	// 2. publish the tarball to the mirror.
	if err := publish(saveTo, &data.Publish, h.mirrorsURL); err != nil {
		return cloudevents.NewReceipt(true, "publish to mirror failed: %v", err)
	}

	// 3. check the package is in the mirror.
	//     printf 'post_check "$(tiup mirror show)/%s-%s-%s-%s.tar.gz" "%s"\n' \
	remoteURL := fmt.Sprintf("%s/%s-%s-%s-%s.tar.gz", h.mirrorsURL, data.Publish.Name, data.Publish.Version, data.Publish.OS, data.Publish.Arch)
	if err := postCheck(saveTo, remoteURL); err != nil {
		return cloudevents.NewReceipt(true, "post check failed: %v", err)
	}
	return cloudevents.ResultACK
}

func (h *Handler) downloadFile(data *PublishRequestEvent) (string, error) {
	switch data.From.Type {
	case FromTypeOci:
		// save to file with `saveTo` param:
		return downloadOCIFile(data.From.Oci)
	case FromTypeHTTP:
		return downloadHTTPFile(data.From.HTTP.URL)
	default:
		return "", nil
	}
}

func publish(file string, info *PublishInfo, to string) error {
	args := []string{"mirror", "publish", info.Name, info.Version, file, info.EntryPoint, "--os", info.OS, "--arch", info.Arch, "--desc", info.Description}
	if info.Standalone {
		args = append(args, "--standalone")
	}
	command := exec.Command("tiup", args...)
	command.Env = os.Environ()
	command.Env = append(command.Env, "TIUP_MIRRORS="+to)
	log.Debug().Any("args", command.Args).Any("env", command.Args).Msg("will execute tiup command")
	output, err := command.Output()
	if err != nil {
		log.Err(err).Msg("failed to execute tiup command")
		return err
	}
	log.Info().
		Str("mirror", to).
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
