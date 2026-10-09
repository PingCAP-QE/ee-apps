package controller

import "testing"

const testArtifactsSHA = "4f25d0e06aec38fbefb5c02513964571eb13eea7"

func TestArtifactsScriptSourceConfigNormalizeDefaults(t *testing.T) {
	t.Parallel()

	got, err := (ArtifactsScriptSourceConfig{}).Normalize()
	if err != nil {
		t.Fatalf("normalize defaults: %v", err)
	}
	if got.URL != DefaultArtifactsScriptRepoURL {
		t.Fatalf("expected default URL %q, got %q", DefaultArtifactsScriptRepoURL, got.URL)
	}
	if got.Revision != DefaultArtifactsScriptRepoRevision {
		t.Fatalf("expected default revision %q, got %q", DefaultArtifactsScriptRepoRevision, got.Revision)
	}
	if got.ExpectedCommit != "" {
		t.Fatalf("expected empty default commit, got %q", got.ExpectedCommit)
	}
}

func TestArtifactsScriptSourceConfigNormalizeAllowsBranch(t *testing.T) {
	t.Parallel()

	got, err := (ArtifactsScriptSourceConfig{
		URL:      DefaultArtifactsScriptRepoURL,
		Revision: "main",
	}).Normalize()
	if err != nil {
		t.Fatalf("expected branch revision to be allowed, got error: %v", err)
	}
	if got.Revision != "main" || got.ExpectedCommit != "" {
		t.Fatalf("unexpected normalized config: %#v", got)
	}
}

func TestArtifactsScriptSourceConfigNormalizeAllowsTagWithoutCommit(t *testing.T) {
	t.Parallel()

	got, err := (ArtifactsScriptSourceConfig{
		URL:      DefaultArtifactsScriptRepoURL,
		Revision: "v2026.4.12",
	}).Normalize()
	if err != nil {
		t.Fatalf("expected tag revision to be allowed, got error: %v", err)
	}
	if got.Revision != "v2026.4.12" || got.ExpectedCommit != "" {
		t.Fatalf("unexpected normalized config: %#v", got)
	}
}

func TestArtifactsScriptSourceConfigNormalizeFullCommitPinsCommit(t *testing.T) {
	t.Parallel()

	got, err := (ArtifactsScriptSourceConfig{
		URL:      DefaultArtifactsScriptRepoURL,
		Revision: testArtifactsSHA,
	}).Normalize()
	if err != nil {
		t.Fatalf("normalize full commit: %v", err)
	}
	if got.ExpectedCommit != testArtifactsSHA {
		t.Fatalf("expected commit %q, got %q", testArtifactsSHA, got.ExpectedCommit)
	}
}

func TestArtifactsScriptSourceConfigNormalizeUsesExpectedCommitWhenRevisionOmitted(t *testing.T) {
	t.Parallel()

	got, err := (ArtifactsScriptSourceConfig{
		URL:            DefaultArtifactsScriptRepoURL,
		ExpectedCommit: testArtifactsSHA,
	}).Normalize()
	if err != nil {
		t.Fatalf("normalize expected-commit-only config: %v", err)
	}
	if got.Revision != testArtifactsSHA {
		t.Fatalf("expected revision %q, got %q", testArtifactsSHA, got.Revision)
	}
}

func TestArtifactsScriptSourceConfigNormalizeRejectsNonSHAExpectedCommit(t *testing.T) {
	t.Parallel()

	_, err := (ArtifactsScriptSourceConfig{
		URL:            DefaultArtifactsScriptRepoURL,
		Revision:       "main",
		ExpectedCommit: "not-a-sha",
	}).Normalize()
	if err == nil {
		t.Fatal("expected non-SHA expected commit to be rejected")
	}
}
