package impl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	goahttp "goa.design/goa/v3/http"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	devbuildcli "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/devbuild/client"
	devbuildsvr "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/devbuild/server"
)

// This opt-in test creates real builds through an isolated EventListener. Its
// template must use the staged pipeline/config and dev GAR, with delivery off.
// SQLite and the HTTP API are local; no production DB or Lark messages are used.
func TestServerlessStagedDevbuild(t *testing.T) {
	listener := os.Getenv("SERVERLESS_STAGE_EVENTLISTENER")
	if listener == "" {
		t.Skip("set SERVERLESS_STAGE_EVENTLISTENER to opt into native staged builds")
	}
	require.Equal(t, "isolated-dev-only", os.Getenv("SERVERLESS_STAGE_CONFIRM"))
	dashboard := os.Getenv("SERVERLESS_STAGE_DASHBOARD")
	require.NotEmpty(t, dashboard)
	token := os.Getenv("GH_TOKEN")
	require.NotEmpty(t, token, "GitHub token must be supplied at runtime")
	product, ref, platform := "cassandra-on-mysql", "branch/feat/serverless-artifacts", "linux"
	if value := os.Getenv("SERVERLESS_STAGE_PRODUCT"); value != "" {
		product = value
	}
	if value := os.Getenv("SERVERLESS_STAGE_GIT_REF"); value != "" {
		ref = value
	}
	if value := os.Getenv("SERVERLESS_STAGE_PLATFORM"); value != "" {
		platform = value
	}
	require.True(t, isServerlessProduct(product))

	logger := zerolog.Nop()
	ce, err := cloudevents.NewClientHTTP(cloudevents.WithTarget(listener))
	require.NoError(t, err)
	s := &devbuildsrvc{
		logger: &logger, dbClient: serverlessTestDB(t),
		productRepoMap:         map[string]string{product: "tidbcloud/" + product},
		ghClient:               github.NewClient(nil).WithAuthToken(token),
		tektonCloudEventClient: ce, dashboardURL: dashboard,
		httpClient: &http.Client{Timeout: 30 * time.Second}, reconcilerSince: time.Hour,
		ociFileDownloadURL: os.Getenv("SERVERLESS_STAGE_DOWNLOAD_URL"),
	}
	mux := goahttp.NewMuxer()
	server := devbuildsvr.New(devbuild.NewEndpoints(s), mux, goahttp.RequestDecoder, goahttp.ResponseEncoder, nil, nil)
	devbuildsvr.Mount(mux, server)
	api := httptest.NewServer(mux)
	defer api.Close()
	address, err := url.Parse(api.URL)
	require.NoError(t, err)
	client := devbuildcli.NewClient(address.Scheme, address.Host, api.Client(), goahttp.RequestEncoder, goahttp.ResponseDecoder, false)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Minute)
	defer cancel()
	requestedVersion := "v99.0.0"
	result, err := client.Create()(ctx, &devbuild.CreatePayload{CreatedBy: new("serverless-validation@pingcap.com"), Request: &devbuild.DevBuildSpec{
		Product: product, Edition: "community", GitRef: ref, Platform: platform, Version: &requestedVersion,
	}})
	require.NoError(t, err)
	build := result.(*devbuild.DevBuild)
	record, err := s.dbClient.DevBuild.Get(ctx, build.ID)
	require.NoError(t, err)
	require.True(t, isHex(record.GitHash))
	require.Len(t, record.TektonStatus.TriggersEventIds, len(parsePlatforms(platform)), "each event must return a correlation ID")
	t.Logf("submitted %s %s %s fixed revision %s event IDs %v", product, ref, platform, record.GitHash, record.TektonStatus.TriggersEventIds)
	lastState := ""
	for {
		s.Reconcile(ctx)
		record, err = s.dbClient.DevBuild.Get(ctx, build.ID)
		require.NoError(t, err)
		state, _ := json.Marshal(record.TektonStatus)
		if string(state) != lastState {
			var progress []string
			for _, pipeline := range record.TektonStatus.Pipelines {
				progress = append(progress, pipeline.Name+":"+pipeline.Status)
			}
			t.Logf("status %s pipelines %s", record.Status, strings.Join(progress, ", "))
			lastState = string(state)
		}
		if isTerminalStatus(record.Status) {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		case <-time.After(15 * time.Second):
		}
	}
	require.Equal(t, "SUCCESS", record.Status)
	require.Len(t, record.TektonStatus.Pipelines, len(parsePlatforms(platform)), "no duplicate PipelineRuns")
	require.Equal(t, requestedVersion, record.Version)
	require.Equal(t, record.GitHash, record.BuildReport.GitHash)
	require.NotEmpty(t, record.BuildReport.PrintedVersion)
	require.NotEqual(t, requestedVersion, record.BuildReport.PrintedVersion)
	for _, image := range record.BuildReport.Images {
		require.Contains(t, image.URL, "/dev/tidbcloud/"+product+"/")
		require.NotContains(t, image.URL, requestedVersion)
	}
	result, err = client.Get()(ctx, &devbuild.GetPayload{ID: build.ID})
	require.NoError(t, err)
	response := result.(*devbuild.DevBuild)
	require.Equal(t, "SUCCESS", string(response.Status.Status))
	require.Equal(t, record.GitHash, derefString(response.Status.BuildReport.GitHash))
	info := buildNotificationInfo(record)
	for i := range info.Binaries {
		info.Binaries[i].URL = notificationDownloadURL(s.ociFileDownloadURL, info.Binaries[i].OciReference)
		info.Binaries[i].Sha256URL = notificationDownloadURL(s.ociFileDownloadURL, info.Binaries[i].Sha256Reference)
	}
	if s.ociFileDownloadURL != "" {
		for _, binary := range response.Status.BuildReport.Binaries {
			require.NotEmpty(t, derefString(binary.URL))
			require.NotEmpty(t, derefString(binary.Sha256URL))
		}
	}
	card, err := NewLarkCardJSON(info)
	require.NoError(t, err)
	require.Contains(t, card, record.GitHash)
	require.Contains(t, card, record.BuildReport.PrintedVersion)
	if output := os.Getenv("SERVERLESS_STAGE_OUTPUT"); output != "" {
		require.NoError(t, os.MkdirAll(output, 0755))
		report, err := json.MarshalIndent(record, "", "  ")
		require.NoError(t, err)
		require.NoError(t, os.WriteFile(filepath.Join(output, "build.json"), report, 0600))
		responseJSON, err := json.MarshalIndent(response, "", "  ")
		require.NoError(t, err)
		require.NoError(t, os.WriteFile(filepath.Join(output, "api-response.json"), responseJSON, 0600))
		require.NoError(t, os.WriteFile(filepath.Join(output, "lark-card.json"), []byte(card), 0600))
	}
}
