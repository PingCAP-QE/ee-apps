package controller

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestCloneArtifactsRepoChecksOutPinnedTagCommit(t *testing.T) {
	t.Parallel()

	repoDir, firstCommit, _, firstTag := createArtifactsRepoFixture(t)
	job := newNativeBuildJob(context.Background(), newTestMacBuild(), ArtifactsScriptSourceConfig{
		URL:            repoDir,
		Revision:       firstTag,
		ExpectedCommit: firstCommit,
	})

	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	defer job.cleanup()

	if err := job.cloneArtifactsRepo(); err != nil {
		t.Fatalf("clone artifacts repo: %v", err)
	}

	headCommit, err := job.gitHeadCommit(job.artifactsRepoDir)
	if err != nil {
		t.Fatalf("resolve cloned HEAD: %v", err)
	}
	if headCommit != firstCommit {
		t.Fatalf("expected cloned HEAD %q, got %q", firstCommit, headCommit)
	}
}

func TestCloneArtifactsRepoRejectsMismatchedExpectedCommit(t *testing.T) {
	t.Parallel()

	repoDir, firstCommit, secondCommit, firstTag := createArtifactsRepoFixture(t)
	job := newNativeBuildJob(context.Background(), newTestMacBuild(), ArtifactsScriptSourceConfig{
		URL:            repoDir,
		Revision:       firstTag,
		ExpectedCommit: secondCommit,
	})

	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	defer job.cleanup()

	err := job.cloneArtifactsRepo()
	if err == nil {
		t.Fatal("expected cloneArtifactsRepo to fail for mismatched commit")
	}
	if !strings.Contains(err.Error(), firstCommit) {
		t.Fatalf("expected error to mention resolved commit %q, got %v", firstCommit, err)
	}
}

func TestCloneArtifactsRepoRejectsBranchRevision(t *testing.T) {
	t.Parallel()

	repoDir, _, firstCommit, _ := createArtifactsRepoFixture(t)
	job := newNativeBuildJob(context.Background(), newTestMacBuild(), ArtifactsScriptSourceConfig{
		URL:            repoDir,
		Revision:       "release/1.0",
		ExpectedCommit: firstCommit,
	})

	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	defer job.cleanup()

	err := job.cloneArtifactsRepo()
	if err == nil {
		t.Fatal("expected cloneArtifactsRepo to reject branch revision")
	}
	if !strings.Contains(err.Error(), "reachable tag or full commit SHA") {
		t.Fatalf("expected branch rejection error, got %v", err)
	}
}

func TestExecStreamsOutputAndReturnsFailureTail(t *testing.T) {
	t.Parallel()

	var stdout, stderr bytes.Buffer
	job := newNativeBuildJob(context.Background(), newTestMacBuild(), ArtifactsScriptSourceConfig{})
	job.stdoutWriter = &stdout
	job.stderrWriter = &stderr

	err := job.exec(exec.Command("sh", "-c", "printf 'stdout-line\\n'; printf 'stderr-line\\n' >&2; exit 7"))
	if err == nil {
		t.Fatal("expected exec to fail")
	}
	if !strings.Contains(err.Error(), "stdout-line") || !strings.Contains(err.Error(), "stderr-line") {
		t.Fatalf("expected error tail to include stdout and stderr, got %v", err)
	}
	if got := stdout.String(); !strings.Contains(got, "stdout-line") {
		t.Fatalf("expected stdout writer to receive streamed output, got %q", got)
	}
	if got := stderr.String(); !strings.Contains(got, "stderr-line") {
		t.Fatalf("expected stderr writer to receive streamed output, got %q", got)
	}
}

func createArtifactsRepoFixture(t *testing.T) (repoDir string, firstCommit string, secondCommit string, firstTag string) {
	t.Helper()

	repoDir = t.TempDir()
	runGit(t, repoDir, "init")
	runGit(t, repoDir, "config", "user.name", "Tester")
	runGit(t, repoDir, "config", "user.email", "tester@example.com")

	scriptPath := filepath.Join(repoDir, "packages", "scripts", "gen-package-artifacts-with-config.sh")
	templatePath := filepath.Join(repoDir, "packages", "packages.yaml.tmpl")
	if err := os.MkdirAll(filepath.Dir(scriptPath), 0o755); err != nil {
		t.Fatalf("mkdir scripts: %v", err)
	}
	if err := os.WriteFile(scriptPath, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write script: %v", err)
	}
	if err := os.WriteFile(templatePath, []byte("template"), 0o644); err != nil {
		t.Fatalf("write template: %v", err)
	}
	runGit(t, repoDir, "add", ".")
	runGit(t, repoDir, "commit", "-m", "first")
	firstCommit = strings.TrimSpace(runGit(t, repoDir, "rev-parse", "HEAD"))
	firstTag = "v1.0.0"
	runGit(t, repoDir, "tag", firstTag)

	if err := os.WriteFile(templatePath, []byte("template-updated"), 0o644); err != nil {
		t.Fatalf("update template: %v", err)
	}
	runGit(t, repoDir, "add", ".")
	runGit(t, repoDir, "commit", "-m", "second")
	secondCommit = strings.TrimSpace(runGit(t, repoDir, "rev-parse", "HEAD"))
	runGit(t, repoDir, "branch", "release/1.0", secondCommit)

	return repoDir, firstCommit, secondCommit, firstTag
}

func newTestMacBuild() buildv1alpha1.MacBuild {
	return buildv1alpha1.MacBuild{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-build",
			Namespace: "default",
		},
		Spec: buildv1alpha1.MacBuildSpec{
			Source: buildv1alpha1.SourceSpec{
				GitRepository: "https://example.invalid/repo.git",
				GitRef:        "main",
			},
			Build: buildv1alpha1.BuildSpec{
				Component: "pd",
				Version:   "v1.0.0",
				Arch:      "amd64",
				Profile:   "release",
			},
			Artifacts: buildv1alpha1.ArtifactsSpec{
				Registry: "hub.pingcap.net/devbuild",
			},
		},
	}
}

func runGit(t *testing.T, dir string, args ...string) string {
	t.Helper()

	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, output)
	}
	return string(output)
}
