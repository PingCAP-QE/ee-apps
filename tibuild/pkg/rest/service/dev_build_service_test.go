package service

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/bndr/gojenkins"
)

type mockRepo struct {
	saved DevBuild
}

func (m *mockRepo) Create(ctx context.Context, req DevBuild) (resp *DevBuild, err error) {
	req.ID = 1
	m.saved = req
	return &req, nil
}
func (m *mockRepo) Get(ctx context.Context, id int) (resp *DevBuild, err error) {
	return &m.saved, nil
}
func (m *mockRepo) Update(ctx context.Context, id int, req DevBuild) (resp *DevBuild, err error) {
	m.saved = req
	return &req, nil
}
func (m *mockRepo) List(ctx context.Context, option DevBuildListOption) (resp []DevBuild, err error) {
	return nil, nil
}

type mockJenkins struct {
	params map[string]string
	resume chan struct{}
	job    *gojenkins.Build
}

func (m *mockJenkins) BuildJob(ctx context.Context, name string, params map[string]string) (int64, error) {
	m.params = params
	return 1, nil
}
func (m mockJenkins) GetBuildNumberFromQueueID(ctx context.Context, queueid int64) (int64, error) {
	if m.resume != nil {
		<-m.resume
	}
	return 2, nil
}

func (mock mockJenkins) GetBuild(ctx context.Context, jobName string, number int64) (*gojenkins.Build, error) {
	return mock.job, nil
}
func (mock mockJenkins) BuildURL(jobName string, number int64) string {
	return fmt.Sprintf("%s/blue/organizations/jenkins/%s/detail/%s/%d/pipeline", "https://cd.pingcap.net/", jobName, jobName, number)
}

type mockTrigger struct {
	dev DevBuild
	err error
}

func (m *mockTrigger) TriggerDevBuild(ctx context.Context, dev DevBuild) error {
	m.dev = dev
	return m.err
}

func TestDevBuildCreate(t *testing.T) {
	mockedJenkins := &mockJenkins{resume: make(chan struct{})}
	mockedRepo := mockRepo{}
	server := DevbuildServer{
		Repo:    &mockedRepo,
		Jenkins: mockedJenkins,
		Now:     time.Now,
		Tekton:  &mockTrigger{},
	}

	t.Run("ok", func(t *testing.T) {
		entity, err := server.Create(context.TODO(),
			DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "master", GithubRepo: "pingcap/tidb"}},
			DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, 1, entity.ID)
		require.Equal(t, BuildStatusPending, entity.Status.Status)
		require.Equal(t, int64(0), entity.Status.PipelineBuildID)
		require.Equal(t, map[string]string{"Edition": "enterprise", "GitRef": "pull/23", "Product": "tidb",
			"Version": "v6.1.2", "PluginGitRef": "master", "IsPushGCR": "false", "IsHotfix": "false", "Features": "",
			"GithubRepo": "pingcap/tidb", "TiBuildID": "1", "BuildEnv": "", "ProductDockerfile": "", "BuilderImg": "",
			"ProductBaseImg": "", "TargetImg": ""}, mockedJenkins.params)
		mockedJenkins.resume <- struct{}{}
		time.Sleep(time.Millisecond)
		require.Equal(t, int64(2), mockedRepo.saved.Status.PipelineBuildID)
	})
	t.Run("auto fill plugin for devbuild", func(t *testing.T) {
		entity, err := server.Create(context.TODO(),
			DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23"}},
			DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, "release-6.1", entity.Spec.PluginGitRef)
	})
	t.Run("auto fill plugin for hotfix", func(t *testing.T) {
		entity, err := server.Create(context.TODO(),
			DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "v6.1.2-20240125-5de58f5", Edition: EditionEnterprise, GitRef: "pull/23", IsHotfix: true}},
			DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, "release-6.1.2", entity.Spec.PluginGitRef)
	})
	t.Run("auto fill fips", func(t *testing.T) {
		entity, err := server.Create(context.TODO(),
			DevBuild{Spec: DevBuildSpec{Product: ProductTikv, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", Features: "fips"}},
			DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, FIPS_BUILD_ENV, entity.Spec.BuildEnv)
		require.Equal(t, FIPS_TIKV_BUILDER, entity.Spec.BuilderImg)
		require.Equal(t, FIPS_TIKV_BASE, entity.Spec.ProductBaseImg)
	})
	t.Run("auto fill fips", func(t *testing.T) {
		entity, err := server.Create(context.TODO(),
			DevBuild{Spec: DevBuildSpec{Product: ProductBr, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", Features: "fips"}},
			DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, FIPS_BUILD_ENV, entity.Spec.BuildEnv)
		require.Equal(t, "https://raw.githubusercontent.com/PingCAP-QE/artifacts/main/dockerfiles/br.Dockerfile", entity.Spec.ProductDockerfile)
	})
	t.Run("bad enterprise plugin", func(t *testing.T) {
		_, err := server.Create(context.TODO(),
			DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "maste"}},
			DevBuildSaveOption{})
		require.ErrorContains(t, err, "pluginGitRef is not valid")
		require.ErrorIs(t, err, ErrBadRequest)
	})
	t.Run("bad product", func(t *testing.T) {
		_, err := server.Create(context.TODO(), DevBuild{Spec: DevBuildSpec{Product: ""}}, DevBuildSaveOption{})
		require.ErrorIs(t, err, ErrBadRequest)
	})

	t.Run("bad version", func(t *testing.T) {
		_, err := server.Create(context.TODO(), DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "av6.1.2", Edition: EditionCommunity}}, DevBuildSaveOption{})
		require.ErrorContains(t, err, "version is not valid")
		require.ErrorIs(t, err, ErrBadRequest)
	})
	t.Run("validate gitRef", func(t *testing.T) {
		obj := DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionCommunity, GitRef: "pull/1234 "}}
		_, err := server.Create(context.TODO(), obj, DevBuildSaveOption{})
		require.ErrorContains(t, err, "gitRef is not valid")
		require.ErrorIs(t, err, ErrBadRequest)

		obj.Spec.GitRef = "pull/abcd"
		_, err = server.Create(context.TODO(), obj, DevBuildSaveOption{})
		require.ErrorContains(t, err, "gitRef is not valid")

		obj.Spec.GitRef = "abcde"
		_, err = server.Create(context.TODO(), obj, DevBuildSaveOption{})
		require.ErrorContains(t, err, "gitRef is not valid")

		obj.Spec.GitRef = "branch/tidb-6.5-with-kv-timeout-feature"
		_, err = server.Create(context.TODO(), obj, DevBuildSaveOption{})
		require.NoError(t, err)

		obj.Spec.GitRef = "release-6.1"
		_, err = server.Create(context.TODO(), obj, DevBuildSaveOption{})
		require.NoError(t, err)
	})

	t.Run("validate target image", func(t *testing.T) {
		obj := DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionCommunity, GitRef: "branch/feature/somefeat"}}
		_, err := server.Create(context.TODO(), obj, DevBuildSaveOption{})
		require.NoError(t, err)

		obj.Spec.TargetImg = "hub.pingcap.net/temp/tidb:somefeat"
		_, err = server.Create(context.TODO(), obj, DevBuildSaveOption{})
		require.ErrorIs(t, err, ErrAuth)

		_, err = server.Create(context.WithValue(context.TODO(), KeyOfApiAccount, "admi"), obj, DevBuildSaveOption{})
		require.ErrorIs(t, err, ErrAuth)

		_, err = server.Create(context.WithValue(context.TODO(), KeyOfApiAccount, AdminApiAccount), obj, DevBuildSaveOption{})
		require.NoError(t, err)

	})

	t.Run("bad githubRepo", func(t *testing.T) {
		_, err := server.Create(context.TODO(), DevBuild{Spec: DevBuildSpec{Product: ProductTidb, GitRef: "pull/23",
			Version: "v6.1.2", Edition: EditionCommunity, GithubRepo: "aa/bb/cc"}}, DevBuildSaveOption{})
		require.ErrorContains(t, err, "githubRepo is not valid")
		require.ErrorIs(t, err, ErrBadRequest)
	})
	t.Run("hotfix ok", func(t *testing.T) {
		_, err := server.Create(context.TODO(), DevBuild{Spec: DevBuildSpec{Product: ProductTidb, GitRef: "branch/master",
			Version: "v6.1.2-20230102", Edition: EditionCommunity, IsHotfix: true}}, DevBuildSaveOption{})
		require.NoError(t, err)
	})
	t.Run("hotfix check", func(t *testing.T) {
		_, err := server.Create(context.TODO(), DevBuild{Spec: DevBuildSpec{Product: ProductTidb, GitRef: "pull/23",
			Version: "v6.1.2", Edition: EditionCommunity, IsHotfix: true}}, DevBuildSaveOption{})
		require.ErrorContains(t, err, "verion must be like v7.0.0-20230102... for hotfix")
		require.ErrorIs(t, err, ErrBadRequest)
	})
}

func TestDevBuildUpdate(t *testing.T) {
	mockedJenkins := &mockJenkins{}
	mockedRepo := mockRepo{}
	server := DevbuildServer{
		Repo:    &mockedRepo,
		Jenkins: mockedJenkins,
		Now:     time.Now,
	}

	t.Run("ok", func(t *testing.T) {
		mockedRepo.saved = DevBuild{Spec: DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "master"}}
		entity, err := server.Update(context.TODO(), 1,
			DevBuild{Spec: DevBuildSpec{Product: ProductBr}, Status: DevBuildStatus{Status: BuildStatusSuccess}},
			DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, BuildStatusSuccess, entity.Status.Status)
		require.Equal(t, ProductTidb, mockedRepo.saved.Spec.Product)
		require.Equal(t, BuildStatusSuccess, mockedRepo.saved.Status.Status)
	})
	t.Run("bad enterprise plugin", func(t *testing.T) {
		_, err := server.Update(context.TODO(),
			1,
			DevBuild{ID: 2},
			DevBuildSaveOption{})
		require.ErrorContains(t, err, "bad id")
		require.ErrorIs(t, err, ErrBadRequest)
	})
	t.Run("with report", func(t *testing.T) {
		req := `
	{
		"status": {
			"status": "SUCCESS",
			"pipelineBuildID": 13,
			"pipelineStartAt": "2023-01-16T20:11:54+08:00",
			"pipelineEndAt": "2023-01-16T20:11:57+08:00",
			"buildReport": {
				"gitHash": "c6ebcec4d7b4d379966bfeb8edd1ab67fc5346b9",
				"images": [
					{
						"platform": "multi-arch",
						"url": "hub.pingcap.net/devbuild/pd:v6.5.0-13"
					}
				],
				"binaries": [
					{
						"platform": "linux/amd64",
						"url": "builds/devbuild/13/pd-linux-amd64.tar.gz",
						"sha256URL": "builds/devbuild/13/pd-linux-amd64.tar.gz.sha256"
					},
					{
						"platform": "linux/arm64",
						"url": "builds/devbuild/13/pd-linux-arm64.tar.gz",
						"sha256URL": "builds/devbuild/13/pd-linux-arm64.tar.gz.sha256"
					}
				]
			}
		}
	}`
		obj := DevBuild{}
		json.Unmarshal([]byte(req), &obj)
		ent, err := server.Update(context.TODO(), 1, obj, DevBuildSaveOption{})
		require.NoError(t, err)
		require.NotNil(t, ent)
		require.Equal(t, int64(13), ent.Status.PipelineBuildID)
	})
}

func TestDevBuildGet(t *testing.T) {
	mockedRepo := mockRepo{}
	mockedJenkins := mockJenkins{}
	server := DevbuildServer{
		Repo:             &mockedRepo,
		Jenkins:          &mockedJenkins,
		Now:              time.Now,
		TektonViewURL:    "http://tekton.net",
		OciFileserverURL: "http://ocidownload.net",
	}

	t.Run("ok", func(t *testing.T) {
		mockedRepo.saved = DevBuild{ID: 1,
			Spec:   DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "master"},
			Status: DevBuildStatus{PipelineBuildID: 4}}
		entity, err := server.Get(context.TODO(), 1, DevBuildGetOption{})
		require.NoError(t, err)
		require.Equal(t, "https://cd.pingcap.net//blue/organizations/jenkins/devbuild/detail/devbuild/4/pipeline", entity.Status.PipelineViewURL)
	})
	t.Run("render oci file", func(t *testing.T) {
		mockedRepo.saved = DevBuild{ID: 1,
			Spec:   DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "master"},
			Status: DevBuildStatus{PipelineBuildID: 4, BuildReport: &BuildReport{Binaries: []BinArtifact{{OciFile: &OciFile{Repo: "repo", Tag: "tag", File: "file"}}}}}}
		entity, err := server.Get(context.TODO(), 1, DevBuildGetOption{})
		require.NoError(t, err)
		require.Equal(t, "http://ocidownload.net/repo?tag=tag&file=file", entity.Status.BuildReport.Binaries[0].URL)
	})
	t.Run("render tekton pipeline", func(t *testing.T) {
		mockedRepo.saved = DevBuild{ID: 1,
			Spec:   DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "master"},
			Status: DevBuildStatus{PipelineBuildID: 4, TektonStatus: &TektonStatus{Pipelines: []TektonPipeline{{Name: "p1"}}}}}
		entity, err := server.Get(context.TODO(), 1, DevBuildGetOption{})
		require.NoError(t, err)
		require.Equal(t, "", entity.Status.TektonStatus.Pipelines[0].URL)
	})
	t.Run("sync", func(t *testing.T) {
		mockedRepo.saved = DevBuild{ID: 1,
			Spec:   DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "master"},
			Status: DevBuildStatus{PipelineBuildID: 4, Status: BuildStatusProcessing}}
		mockedJenkins.job = &gojenkins.Build{Raw: &gojenkins.BuildResponse{Result: "PROCCESSING", Timestamp: time.Unix(1, 0).UnixMilli()}}
		entity, err := server.Get(context.TODO(), 1, DevBuildGetOption{Sync: true})
		require.NoError(t, err)
		require.Equal(t, time.Unix(1, 0).Local(), *entity.Status.PipelineStartAt)
		require.Equal(t, "https://cd.pingcap.net//blue/organizations/jenkins/devbuild/detail/devbuild/4/pipeline", entity.Status.PipelineViewURL)
	})
}

func TestMergeTektonStatus(t *testing.T) {
	mockedRepo := mockRepo{}
	server := DevbuildServer{
		Repo: &mockedRepo,
		Now:  time.Now,
	}

	t.Run("ok", func(t *testing.T) {
		mockedRepo.saved = DevBuild{ID: 1,
			Spec: DevBuildSpec{
				Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise,
				GitRef: "pull/23", PluginGitRef: "master", PipelineEngine: TektonEngine,
			},
			Status: DevBuildStatus{TektonStatus: &TektonStatus{Pipelines: []TektonPipeline{
				{Name: "p1", Platform: LinuxAmd64, Status: BuildStatusSuccess},
				{Name: "p2", Platform: LinuxArm64, Status: BuildStatusProcessing},
			}}}}
		pipelinerun := TektonPipeline{Name: "p2", Platform: LinuxArm64, Status: BuildStatusSuccess, OciArtifacts: []OciArtifact{{Repo: "repo", Tag: "tag", Files: []string{"file1.tar.gz", "file2.tar.gz"}}}}
		entity, err := server.MergeTektonStatus(context.TODO(), 1, pipelinerun, DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, BuildStatusSuccess, entity.Status.Status)
		require.Equal(t, 2, len(entity.Status.BuildReport.Binaries))
		entity, err = server.MergeTektonStatus(context.TODO(), 1, pipelinerun, DevBuildSaveOption{})
		require.NoError(t, err)
		require.Equal(t, 2, len(entity.Status.BuildReport.Binaries))
	})
}

func TestDevBuildRerun(t *testing.T) {
	mockedRepo := mockRepo{}
	server := DevbuildServer{
		Repo:    &mockedRepo,
		Jenkins: &mockJenkins{},
		Now:     time.Now,
		Tekton:  &mockTrigger{},
	}
	mockedRepo.saved = DevBuild{ID: 2,
		Spec:   DevBuildSpec{Product: ProductTidb, Version: "v6.1.2", Edition: EditionEnterprise, GitRef: "pull/23", PluginGitRef: "master"},
		Status: DevBuildStatus{PipelineBuildID: 4}}
	entity, err := server.Rerun(context.TODO(), 1, DevBuildSaveOption{})
	require.NoError(t, err)
	require.Equal(t, 1, entity.ID)
	require.Equal(t, "v6.1.2", entity.Spec.Version)
}

func TestTektonStatusMerge(t *testing.T) {
	starttime := time.Unix(1, 0)
	endtime := time.Unix(20, 0)

	t.Run("success", func(t *testing.T) {
		tekton := &TektonStatus{
			Pipelines: []TektonPipeline{
				{Name: "pipelinerun1", Status: BuildStatusSuccess, Platform: LinuxAmd64,
					StartAt:      &starttime,
					EndAt:        &endtime,
					OciArtifacts: []OciArtifact{{Repo: "harbor.net/org/repo", Tag: "master", Files: []string{"a.tar.gz", "b.tar.gz"}}},
					Images:       []ImageArtifact{{URL: "harbor.net/org/image:tag1"}}},
				{Name: "pipelinerun2", Status: BuildStatusSuccess, Platform: LinuxAmd64,
					StartAt:      &starttime,
					EndAt:        &endtime,
					Images:       []ImageArtifact{{URL: "harbor.net/org/image:tag2"}},
					OciArtifacts: []OciArtifact{{Repo: "harbor.net/org/repo", Tag: "master", Files: []string{"c.tar.gz", "d.tar.gz"}}}},
			},
		}
		status := &DevBuildStatus{TektonStatus: tekton}
		computeTektonStatus(tekton, status)
		require.Equal(t, BuildStatusSuccess, status.Status)
		t.Logf("PipelineStartAt: %v", status.PipelineStartAt)
		t.Logf("PipelineEndAt: %v", status.PipelineEndAt)
		require.Equal(t, endtime.Sub(starttime), status.PipelineEndAt.Sub(*status.PipelineStartAt))
		require.Equal(t, 2, len(status.BuildReport.Images))
		require.Equal(t, 4, len(status.BuildReport.Binaries))
	})

	t.Run("processing", func(t *testing.T) {
		tekton := &TektonStatus{
			Pipelines: []TektonPipeline{
				{Name: "pipelinerun1", Status: BuildStatusSuccess, Platform: LinuxAmd64,
					StartAt:      &starttime,
					OciArtifacts: []OciArtifact{{Repo: "harbor.net/org/repo", Files: []string{"a.tar.gz", "b.tar.gz"}}},
					Images:       []ImageArtifact{{URL: "harbor.net/org/image:tag1"}}},
				{Name: "pipelinerun2", Status: BuildStatusProcessing, Platform: LinuxArm64,
					StartAt:      &starttime,
					EndAt:        &endtime,
					OciArtifacts: []OciArtifact{{Repo: "harbor.net/org/repo", Files: []string{"c.tar.gz", "d.tar.gz"}}}},
			},
		}
		status := &DevBuildStatus{TektonStatus: tekton}
		computeTektonStatus(tekton, status)
		require.Equal(t, BuildStatusProcessing, status.Status)
	})
	t.Run("processing", func(t *testing.T) {
		tekton := &TektonStatus{
			Pipelines: []TektonPipeline{
				{Name: "pipelinerun1", Status: BuildStatusSuccess, Platform: LinuxAmd64,
					StartAt:      &starttime,
					OciArtifacts: []OciArtifact{{Repo: "harbor.net/org/repo", Tag: "latest", Files: []string{"a.tar.gz", "b.tar.gz"}}},
					Images:       []ImageArtifact{{URL: "harbor.net/org/image:tag1"}}},
				{Name: "pipelinerun2", Status: BuildStatusFailure, Platform: LinuxArm64,
					StartAt:      &starttime,
					EndAt:        &endtime,
					OciArtifacts: []OciArtifact{{Repo: "harbor.net/org/repo", Files: []string{"c.tar.gz", "d.tar.gz"}}}},
			},
		}
		status := &DevBuildStatus{TektonStatus: tekton}
		computeTektonStatus(tekton, status)
		require.Equal(t, BuildStatusFailure, status.Status)
	})

}

func TestOciArtifactToFiles(t *testing.T) {
	files := ociArtifactToFiles(LinuxAmd64,
		OciArtifact{Repo: "repo", Tag: "tag",
			Files: []string{
				"f1.tar.gz",
				"f1.tar.gz.sha256",
				"f2.tar.gz.sha256",
				"f2.tar.gz"},
		},
	)
	require.Equal(t, 2, len(files))
	require.NotNil(t, files[0].Sha256OciFile)
	require.NotNil(t, files[1].Sha256OciFile)
}

func TestValidateReq(t *testing.T) {
	tests := []struct {
		name    string
		req     DevBuild
		wantErr bool
		errMsg  string
	}{
		{
			name: "valid request with jenkins pipeline",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: false,
		},
		{
			name: "valid request with tekton pipeline",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: TektonEngine,
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: false,
		},
		{
			name: "invalid product",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        "invalid-product",
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: true,
			errMsg:  "product is not valid",
		},
		{
			name: "invalid edition for jenkins",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionExperiment, // Not in InvalidEditionForJenkins
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: true,
			errMsg:  "edition is not valid for jenkins engine",
		},
		{
			name: "invalid edition for tekton",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        "unknown", // Not in InvalidEditionForTekton
					PipelineEngine: TektonEngine,
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: true,
			errMsg:  "edition is not valid for tekton engine",
		},
		{
			name: "invalid pipeline engine",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: "invalid_engine",
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: true,
			errMsg:  "pipeline engine is not valid",
		},
		{
			name: "invalid version",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "invalid-version",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: true,
			errMsg:  "version is not valid",
		},
		{
			name: "invalid gitRef",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "invalid/gitref",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
				},
			},
			wantErr: true,
			errMsg:  "gitRef is not valid",
		},
		{
			name: "invalid githubRepo",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap_tidb", // using _ instead of /
				},
			},
			wantErr: true,
			errMsg:  "githubRepo is not valid, should be like org/repo",
		},
		{
			name: "missing pluginGitRef for enterprise edition",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionEnterprise,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
					PluginGitRef:   "",
				},
			},
			wantErr: true,
			errMsg:  "pluginGitRef is not valid",
		},
		{
			name: "valid enterprise edition with pluginGitRef",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionEnterprise,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
					PluginGitRef:   "branch/enterprise",
				},
			},
			wantErr: false,
		},
		{
			name: "hotfix with invalid version format",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
					IsHotfix:       true,
				},
			},
			wantErr: true,
			errMsg:  "verion must be like v7.0.0-20230102... for hotfix",
		},
		{
			name: "hotfix with valid version but targetImg set",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0-20230102",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
					IsHotfix:       true,
					TargetImg:      "example/image:tag",
				},
			},
			wantErr: true,
			errMsg:  "target image shall be empty for hotfix",
		},
		{
			name: "valid hotfix",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0-20230102",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
					IsHotfix:       true,
				},
			},
			wantErr: false,
		},
		{
			name: "jenkins engine with platform set",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: JenkinsEngine,
					GithubRepo:     "pingcap/tidb",
					Platform:       "linux/amd64",
				},
			},
			wantErr: true,
			errMsg:  "cannot set platform when pipeline engine is Jenkins",
		},
		{
			name: "tekton engine with platform set",
			req: DevBuild{
				Spec: DevBuildSpec{
					Product:        ProductTidb,
					GitRef:         "branch/main",
					Version:        "v6.5.0",
					Edition:        EditionCommunity,
					PipelineEngine: TektonEngine,
					GithubRepo:     "pingcap/tidb",
					Platform:       "linux/amd64",
				},
			},
			wantErr: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateReq(tt.req)

			// Check if error is expected
			if (err != nil) != tt.wantErr {
				t.Errorf("validateReq() error = %v, wantErr %v", err, tt.wantErr)
				return
			}

			// Check error message if error is expected
			if tt.wantErr != (err != nil) {
				t.Errorf("validateReq() error message = %v, want error: %v", err.Error(), tt.wantErr)
			}
		})
	}
}
