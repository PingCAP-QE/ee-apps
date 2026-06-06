package controller

import "testing"

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
	if got.ExpectedCommit != DefaultArtifactsScriptExpectedCommit {
		t.Fatalf("expected default commit %q, got %q", DefaultArtifactsScriptExpectedCommit, got.ExpectedCommit)
	}
}

func TestArtifactsScriptSourceConfigNormalizeRejectsMutableBranch(t *testing.T) {
	t.Parallel()

	_, err := (ArtifactsScriptSourceConfig{
		URL:            DefaultArtifactsScriptRepoURL,
		Revision:       "main",
		ExpectedCommit: DefaultArtifactsScriptExpectedCommit,
	}).Normalize()
	if err == nil {
		t.Fatal("expected mutable branch revision to be rejected")
	}
}

func TestArtifactsScriptSourceConfigNormalizeRequiresCommitForTag(t *testing.T) {
	t.Parallel()

	_, err := (ArtifactsScriptSourceConfig{
		URL:      DefaultArtifactsScriptRepoURL,
		Revision: "v2026.4.12",
	}).Normalize()
	if err == nil {
		t.Fatal("expected tag revision without expected commit to be rejected")
	}
}

func TestArtifactsScriptSourceConfigNormalizeAllowsFullCommitWithoutExplicitCommit(t *testing.T) {
	t.Parallel()

	got, err := (ArtifactsScriptSourceConfig{
		URL:      DefaultArtifactsScriptRepoURL,
		Revision: DefaultArtifactsScriptExpectedCommit,
	}).Normalize()
	if err != nil {
		t.Fatalf("normalize full commit: %v", err)
	}
	if got.ExpectedCommit != DefaultArtifactsScriptExpectedCommit {
		t.Fatalf("expected commit %q, got %q", DefaultArtifactsScriptExpectedCommit, got.ExpectedCommit)
	}
}

func TestArtifactsScriptSourceConfigNormalizeUsesExpectedCommitWhenRevisionOmitted(t *testing.T) {
	t.Parallel()

	got, err := (ArtifactsScriptSourceConfig{
		URL:            DefaultArtifactsScriptRepoURL,
		ExpectedCommit: DefaultArtifactsScriptExpectedCommit,
	}).Normalize()
	if err != nil {
		t.Fatalf("normalize expected-commit-only config: %v", err)
	}
	if got.Revision != DefaultArtifactsScriptExpectedCommit {
		t.Fatalf("expected revision %q, got %q", DefaultArtifactsScriptExpectedCommit, got.Revision)
	}
}
