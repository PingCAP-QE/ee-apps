package impl

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	tknv1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
)

func TestNewDevBuildCloudEvent_PluginGitRef(t *testing.T) {
	s := &devbuildsrvc{}
	record := &ent.DevBuild{
		ID:           1,
		CreatedBy:    "test@pingcap.com",
		Product:      "tidb",
		Edition:      "community",
		GithubRepo:   "pingcap/tidb",
		GitRef:       "branch/master",
		PluginGitRef: "release-8.5.4",
	}

	event, err := s.newDevBuildCloudEvent(record, LinuxAmd64, gitRefInfo{Ref: "refs/heads/master", SHA: "0123456789abcdef0123456789abcdef01234567"})
	require.NoError(t, err)
	require.Equal(t, "release-8.5.4", event.Extensions()["paramplugingitref"])
}

func TestNewDevBuildCloudEvent_PluginGitRefEmpty(t *testing.T) {
	s := &devbuildsrvc{}
	record := &ent.DevBuild{
		ID:           1,
		CreatedBy:    "test@pingcap.com",
		Product:      "tidb",
		Edition:      "community",
		GithubRepo:   "pingcap/tidb",
		GitRef:       "branch/master",
		PluginGitRef: "",
	}

	event, err := s.newDevBuildCloudEvent(record, LinuxAmd64, gitRefInfo{Ref: "refs/heads/master", SHA: "0123456789abcdef0123456789abcdef01234567"})
	require.NoError(t, err)
	_, ok := event.Extensions()["paramplugingitref"]
	require.False(t, ok)
}

func TestOciArtifactToBinArtifacts_Platform(t *testing.T) {
	// binaries carry the platform of the pipeline run that produced them
	bins := ociArtifactToBinArtifacts("devbuild/1", "v8.5.5-20260127-69f3866",
		[]string{"pd-linux-amd64.tar.gz", "pd-linux-amd64.tar.gz.sha256"}, "linux/amd64")
	require.Len(t, bins, 1)
	require.NotNil(t, bins[0].Platform)
	require.Equal(t, "linux/amd64", *bins[0].Platform)
	require.NotNil(t, bins[0].Sha256OCIFile)

	// empty platform stays nil (no platform reported)
	bins = ociArtifactToBinArtifacts("devbuild/1", "v8.5.5-20260127-69f3866",
		[]string{"pd-linux-amd64.tar.gz"}, "")
	require.Len(t, bins, 1)
	require.Nil(t, bins[0].Platform)
}

func TestExtractArtifactsFromResults_MultiArch(t *testing.T) {
	results := []tknv1.PipelineRunResult{
		{
			Name: "pushed-images",
			Value: tknv1.ParamValue{
				Type:      tknv1.ParamTypeString,
				StringVal: "images:\n  - repo: us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/tidb-server\n    tag: v8.5.7-20260819-5bc0f93_linux_amd64\n    multi_arch_tags:\n      - v8.5.7-20260819-5bc0f93\n  - repo: us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/br\n    tag: v8.5.7-20260819-5bc0f93_linux_amd64\n    multi_arch_tags:\n      - v8.5.7-20260819-5bc0f93\n",
			},
		},
	}
	params := tknv1.Params{
		{Name: "os", Value: tknv1.ParamValue{Type: tknv1.ParamTypeString, StringVal: "linux"}},
		{Name: "arch", Value: tknv1.ParamValue{Type: tknv1.ParamTypeString, StringVal: "amd64"}},
	}

	ociArtifacts, images := extractArtifactsFromResults(results, params)
	require.Empty(t, ociArtifacts)
	require.Len(t, images, 4)

	require.Equal(t, "linux/amd64", images[0].Platform)
	require.Equal(t, "us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/tidb-server:v8.5.7-20260819-5bc0f93_linux_amd64", images[0].URL)

	require.Equal(t, "multi-arch", images[1].Platform)
	require.Equal(t, "us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/tidb-server:v8.5.7-20260819-5bc0f93", images[1].URL)

	require.Equal(t, "linux/amd64", images[2].Platform)
	require.Equal(t, "us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/br:v8.5.7-20260819-5bc0f93_linux_amd64", images[2].URL)

	require.Equal(t, "multi-arch", images[3].Platform)
	require.Equal(t, "us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/br:v8.5.7-20260819-5bc0f93", images[3].URL)
}

func TestBuildBuildReport_MultiArch(t *testing.T) {
	pr := tknv1.PipelineRun{
		ObjectMeta: metav1.ObjectMeta{Name: "bp-tidb-release-linux-amd64-4hkrr"},
		Spec: tknv1.PipelineRunSpec{
			Params: tknv1.Params{
				{Name: "os", Value: tknv1.ParamValue{Type: tknv1.ParamTypeString, StringVal: "linux"}},
				{Name: "arch", Value: tknv1.ParamValue{Type: tknv1.ParamTypeString, StringVal: "amd64"}},
			},
		},
		Status: tknv1.PipelineRunStatus{
			PipelineRunStatusFields: tknv1.PipelineRunStatusFields{
				Results: []tknv1.PipelineRunResult{
					{
						Name: "pushed-images",
						Value: tknv1.ParamValue{
							Type:      tknv1.ParamTypeString,
							StringVal: "images:\n  - repo: us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/tidb-server\n    tag: v8.5.7-20260819-5bc0f93_linux_amd64\n    multi_arch_tags:\n      - v8.5.7-20260819-5bc0f93\n",
						},
					},
				},
			},
		},
	}

	report := buildBuildReport([]tknv1.PipelineRun{pr})
	require.NotNil(t, report)
	require.Len(t, report.Images, 2)
	require.Equal(t, "linux/amd64", report.Images[0].Platform)
	require.Equal(t, "multi-arch", report.Images[1].Platform)
	require.Equal(t, "us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/tidb-server:v8.5.7-20260819-5bc0f93", report.Images[1].URL)
}

func TestBuildBuildReport_MultiArchDedup(t *testing.T) {
	// Both pipelines report the same multi-arch tags -> deduped in the report.
	makePR := func(name, arch string) tknv1.PipelineRun {
		return tknv1.PipelineRun{
			ObjectMeta: metav1.ObjectMeta{Name: name},
			Spec: tknv1.PipelineRunSpec{
				Params: tknv1.Params{
					{Name: "os", Value: tknv1.ParamValue{Type: tknv1.ParamTypeString, StringVal: "linux"}},
					{Name: "arch", Value: tknv1.ParamValue{Type: tknv1.ParamTypeString, StringVal: arch}},
				},
			},
			Status: tknv1.PipelineRunStatus{
				PipelineRunStatusFields: tknv1.PipelineRunStatusFields{
					Results: []tknv1.PipelineRunResult{
						{
							Name: "pushed-images",
							Value: tknv1.ParamValue{
								Type:      tknv1.ParamTypeString,
								StringVal: "images:\n  - repo: us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/tidb-server\n    tag: v8.5.7-20260819-5bc0f93_linux_" + arch + "\n    multi_arch_tags:\n      - v8.5.7-20260819-5bc0f93\n",
							},
						},
					},
				},
			},
		}
	}

	report := buildBuildReport([]tknv1.PipelineRun{makePR("bp-tidb-release-linux-amd64-aaa", "amd64"), makePR("bp-tidb-release-linux-arm64-bbb", "arm64")})
	require.NotNil(t, report)
	require.Len(t, report.Images, 3) // amd64 + arm64 + one multi-arch (deduped)
	platforms := []string{}
	for _, img := range report.Images {
		platforms = append(platforms, img.Platform)
	}
	require.ElementsMatch(t, []string{"linux/amd64", "linux/arm64", "multi-arch"}, platforms)
	var multiArch []schema.ImageArtifact
	for _, img := range report.Images {
		if img.Platform == "multi-arch" {
			multiArch = append(multiArch, img)
		}
	}
	require.Len(t, multiArch, 1)
	require.Equal(t, "us-docker.pkg.dev/pingcap-testing-account/hotfix/pingcap/tidb/images/tidb-server:v8.5.7-20260819-5bc0f93", multiArch[0].URL)
}

func TestNewDevBuildCloudEvent_RefKinds(t *testing.T) {
	const sha = "0123456789abcdef0123456789abcdef01234567"

	tests := []struct {
		name     string
		gitRef   string
		info     gitRefInfo
		wantType string
		wantData []string
	}{
		{
			name:     "branch",
			gitRef:   "branch/master",
			info:     gitRefInfo{Ref: "refs/heads/master", SHA: sha},
			wantType: ceTypeFakeGHPushDevBuild,
			wantData: []string{`"ref":"refs/heads/master"`, `"after":"` + sha + `"`},
		},
		{
			name:     "tag",
			gitRef:   "tag/v9.0.0",
			info:     gitRefInfo{Ref: "refs/tags/v9.0.0", SHA: sha},
			wantType: ceTypeFakeGHCreateDevBuild,
			wantData: []string{`"ref":"v9.0.0"`, `"ref_type":"tag"`},
		},
		{
			name:     "pull",
			gitRef:   "pull/42",
			info:     gitRefInfo{Ref: "refs/heads/main", SHA: sha, BaseRef: "main"},
			wantType: ceTypeFakeGHPrDevBuild,
			wantData: []string{`"number":42`, `"head":{"sha":"` + sha + `"`, `"base":{"ref":"main"`},
		},
		{
			name:     "commit",
			gitRef:   "commit/" + sha,
			info:     gitRefInfo{Ref: sha, SHA: sha},
			wantType: ceTypeFakeGHPushDevBuild,
			wantData: []string{`"ref":"` + sha + `"`, `"after":"` + sha + `"`},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			s := &devbuildsrvc{}
			record := &ent.DevBuild{
				ID:         7,
				CreatedBy:  "test@pingcap.com",
				Product:    "pd",
				Edition:    "community",
				GithubRepo: "pingcap/pd",
				GitRef:     tt.gitRef,
			}

			event, err := s.newDevBuildCloudEvent(record, LinuxAmd64, tt.info)
			require.NoError(t, err)
			require.Equal(t, tt.wantType, event.Type())
			for _, want := range tt.wantData {
				require.Contains(t, string(event.Data()), want)
			}
			require.Equal(t, "7", event.Subject())
		})
	}
}

func TestGitRefInfoFromRecord(t *testing.T) {
	const sha = "0123456789abcdef0123456789abcdef01234567"

	tests := []struct {
		name   string
		gitRef string
		hash   string
		want   gitRefInfo
	}{
		{name: "branch", gitRef: "branch/master", hash: sha, want: gitRefInfo{Ref: "refs/heads/master", SHA: sha}},
		{name: "tag", gitRef: "tag/v9.0.0", hash: sha, want: gitRefInfo{Ref: "refs/tags/v9.0.0", SHA: sha}},
		{name: "commit", gitRef: "commit/" + sha, hash: sha, want: gitRefInfo{Ref: sha, SHA: sha}},
		{name: "raw sha", gitRef: sha, hash: sha, want: gitRefInfo{Ref: sha, SHA: sha}},
		{name: "pull keeps no base ref", gitRef: "pull/42", hash: sha, want: gitRefInfo{SHA: sha}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := gitRefInfoFromRecord(&ent.DevBuild{GitRef: tt.gitRef, GitHash: tt.hash})
			require.Equal(t, tt.want, got)
		})
	}
}

func TestTriggerTknBuild_CloudEventFailure(t *testing.T) {
	// A closed listener forces a transport error, which the client reports as a
	// non-ACK result.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	target := server.URL
	server.Close()

	client, err := cloudevents.NewClientHTTP(cloudevents.WithTarget(target))
	require.NoError(t, err)

	logger := zerolog.New(io.Discard)
	s := &devbuildsrvc{logger: &logger, tektonCloudEventClient: client}
	record := &ent.DevBuild{
		ID:         9,
		GithubRepo: "pingcap/pd",
		GitRef:     "branch/master",
		Platform:   LinuxAmd64,
	}

	_, err = s.triggerTknBuild(context.Background(), record, gitRefInfo{Ref: "refs/heads/master", SHA: "sha"})
	require.Error(t, err)
}
