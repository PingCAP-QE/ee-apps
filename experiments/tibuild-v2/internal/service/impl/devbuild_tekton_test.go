package impl

import (
	"testing"

	tknv1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/stretchr/testify/require"
)

func TestNewDevBuildCloudEvent_PluginGitRef(t *testing.T) {
	s := &devbuildsrvc{}
	record := &ent.DevBuild{
		ID:          1,
		CreatedBy:   "test@pingcap.com",
		Product:     "tidb",
		Edition:     "community",
		GithubRepo:  "pingcap/tidb",
		GitRef:      "branch/master",
		PluginGitRef: "release-8.5.4",
	}

	event, err := s.newDevBuildCloudEvent(record, LinuxAmd64)
	require.NoError(t, err)
	require.Equal(t, "release-8.5.4", event.Extensions()["paramplugingitref"])
}

func TestNewDevBuildCloudEvent_PluginGitRefEmpty(t *testing.T) {
	s := &devbuildsrvc{}
	record := &ent.DevBuild{
		ID:          1,
		CreatedBy:   "test@pingcap.com",
		Product:     "tidb",
		Edition:     "community",
		GithubRepo:  "pingcap/tidb",
		GitRef:      "branch/master",
		PluginGitRef: "",
	}

	event, err := s.newDevBuildCloudEvent(record, LinuxAmd64)
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
