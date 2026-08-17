package tidbcloud

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
)

const (
	testRepo      = "tidbcloud/cloud-storage-engine"
	testCommitSHA = "ff685313539da0046e0951a02f8de2f0e2791602"
	testImage     = "us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31"
	testGitTag    = "v8.5.4-nextgen.202510.31"
)

func testKernelImageMeta(tag bool) kernelImageMeta {
	meta := kernelImageMeta{
		Repo:      testRepo,
		CommitSHA: testCommitSHA,
	}
	if tag {
		meta.GitTag = testGitTag
	} else {
		meta.Branch = "release-nextgen-20251015"
	}
	return meta
}

// newKernelImageTestSvc starts the ops callback mock and returns the service with
// an injectable image metadata reader and an optional tibuild metadata mock.
func newKernelImageTestSvc(t *testing.T, meta kernelImageMeta, opsHandler func(w http.ResponseWriter, r *http.Request), tibuildHandler func(w http.ResponseWriter, r *http.Request)) *tidbcloudsrvc {
	t.Helper()
	opsSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if opsHandler != nil {
			opsHandler(w, r)
		} else {
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(opsSrv.Close)

	var tibuildCfg TiBuildV2Config
	if tibuildHandler != nil {
		tibuildSrv := httptest.NewServer(http.HandlerFunc(tibuildHandler))
		t.Cleanup(tibuildSrv.Close)
		tibuildCfg = TiBuildV2Config{APIBaseURL: tibuildSrv.URL}
	}

	logger := zerolog.New(io.Discard)
	return &tidbcloudsrvc{
		BaseService: &share.BaseService{Logger: &logger},
		opsCfg: &OpsConfig{
			TiBuildV2: tibuildCfg,
			Stages: map[string]OpsStageConfig{
				"dev": {APIBaseURL: opsSrv.URL, APIKey: "test-key"},
			},
		},
		kernelImageMetaReader: func(_ context.Context, _ string) kernelImageMeta {
			return meta
		},
	}
}

func tibuildMetadataHandler(t *testing.T, tag, author, releaseID, changeID string) func(w http.ResponseWriter, r *http.Request) {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/hotfix/tidbx/tag" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		if r.URL.Query().Get("repo") != testRepo || r.URL.Query().Get("tag") != tag {
			http.Error(w, "unexpected query", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"repo":"` + testRepo + `","commit":"` + testCommitSHA + `","tag":"` + tag + `","author":"` + author + `","meta":{"ops_req":{"release_id":"` + releaseID + `","change_id":"` + changeID + `"}}}`))
	}
}

func TestRequestSyncKernelImageWithTag(t *testing.T) {
	var got OpsKernelImageSyncRequest
	s := newKernelImageTestSvc(t, testKernelImageMeta(true), func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "test-key" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.Method != http.MethodPost || r.URL.Path != "/devops/kernel-images/build-callback" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &got); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}, tibuildMetadataHandler(t, testGitTag, "xieyujie@pingcap.com", "123", "456"))

	p := &tidbcloud.RequestSyncKernelImagePayload{Stage: "dev", Image: testImage}
	res, err := s.RequestSyncKernelImage(context.Background(), p)
	if err != nil {
		t.Fatalf("RequestSyncKernelImage() error = %v", err)
	}
	if res != "ok" {
		t.Fatalf("RequestSyncKernelImage() res = %q, want %q", res, "ok")
	}

	want := OpsKernelImageSyncRequest{
		SourceApplicant:  "xieyujie@pingcap.com",
		SourceReleaseID:  "123",
		SourceChangeID:   "456",
		Repo:             testRepo,
		CommitSHA:        testCommitSHA,
		GitTag:           testGitTag,
		SourceRepository: "us.gcr.io/pingcap-public/tidbx/tikv",
		SourceTag:        testGitTag,
	}
	if got != want {
		t.Fatalf("request body = %+v, want %+v", got, want)
	}
}

func TestRequestSyncKernelImageWithBranch(t *testing.T) {
	var got OpsKernelImageSyncRequest
	s := newKernelImageTestSvc(t, testKernelImageMeta(false), func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}, tibuildMetadataHandler(t, "release-nextgen-20251015", "xieyujie@pingcap.com", "", ""))

	p := &tidbcloud.RequestSyncKernelImagePayload{
		Stage: "dev",
		Image: "us.gcr.io/pingcap-public/tidbx/tikv:release-nextgen-20251015",
	}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err != nil {
		t.Fatalf("RequestSyncKernelImage() error = %v", err)
	}
	if got.GitTag != "" || got.Branch != "release-nextgen-20251015" {
		t.Fatalf("request body = %+v, want branch set and git_tag empty", got)
	}
	if got.SourceReleaseID != "" || got.SourceChangeID != "" {
		t.Fatalf("request body = %+v, want optional ids omitted", got)
	}
}

func TestClassifyKernelImageRef(t *testing.T) {
	tests := []struct {
		ref    string
		gitTag string
		branch string
	}{
		{ref: "v8.5.4-nextgen.202510.31", gitTag: "v8.5.4-nextgen.202510.31"},
		{ref: "v26.3.1-nextgen", gitTag: "v26.3.1-nextgen"},
		{ref: "v26.3.1", gitTag: "v26.3.1"},
		{ref: "release-nextgen-20251015", branch: "release-nextgen-20251015"},
		{ref: "master", branch: "master"},
		{ref: "  v8.5.4-nextgen.202510.31  ", gitTag: "v8.5.4-nextgen.202510.31"},
		{ref: "", gitTag: "", branch: ""},
	}

	for _, tt := range tests {
		gitTag, branch := classifyKernelImageRef(tt.ref)
		if gitTag != tt.gitTag || branch != tt.branch {
			t.Fatalf("classifyKernelImageRef(%q) = (%q, %q), want (%q, %q)", tt.ref, gitTag, branch, tt.gitTag, tt.branch)
		}
	}
}

func TestRequestSyncKernelImageErrors(t *testing.T) {
	logger := zerolog.New(io.Discard)
	s := &tidbcloudsrvc{BaseService: &share.BaseService{Logger: &logger}}

	if _, err := s.RequestSyncKernelImage(context.Background(), nil); err == nil {
		t.Fatal("expected error for nil payload")
	}

	p := &tidbcloud.RequestSyncKernelImagePayload{Stage: "  "}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err == nil {
		t.Fatal("expected error for empty stage")
	}

	p = &tidbcloud.RequestSyncKernelImagePayload{Stage: "dev"}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err == nil {
		t.Fatal("expected error for empty image")
	}

	p = &tidbcloud.RequestSyncKernelImagePayload{Stage: "dev", Image: testImage}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err == nil {
		t.Fatal("expected error when ops config is not configured")
	}

	s.opsCfg = &OpsConfig{Stages: map[string]OpsStageConfig{"dev": {APIBaseURL: "http://127.0.0.1:1", APIKey: "test-key"}}}

	s.kernelImageMetaReader = func(_ context.Context, _ string) kernelImageMeta {
		return kernelImageMeta{}
	}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err == nil {
		t.Fatal("expected error when image metadata missing")
	}

	s.kernelImageMetaReader = func(_ context.Context, _ string) kernelImageMeta {
		return kernelImageMeta{Repo: testRepo, CommitSHA: testCommitSHA}
	}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err == nil {
		t.Fatal("expected error when git ref label missing")
	}

	s.kernelImageMetaReader = func(_ context.Context, _ string) kernelImageMeta {
		return testKernelImageMeta(true)
	}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err == nil {
		t.Fatal("expected error when stage not found in ops config")
	}

	s.opsCfg = &OpsConfig{Stages: map[string]OpsStageConfig{"dev": {APIBaseURL: "http://127.0.0.1:1", APIKey: "test-key"}}}
	if _, err := s.RequestSyncKernelImage(context.Background(), p); err == nil {
		t.Fatal("expected error when tibuild metadata unavailable and applicant unresolvable")
	}
}
