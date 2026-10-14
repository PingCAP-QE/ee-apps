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

func TestCloneArtifactsRepoChecksOutBranchRevision(t *testing.T) {
	t.Parallel()

	repoDir, _, secondCommit, _ := createArtifactsRepoFixture(t)
	job := newNativeBuildJob(context.Background(), newTestMacBuild(), ArtifactsScriptSourceConfig{
		URL:      repoDir,
		Revision: "release/1.0",
	})

	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	defer job.cleanup()

	if err := job.cloneArtifactsRepo(); err != nil {
		t.Fatalf("expected branch revision to be accepted, got: %v", err)
	}

	headCommit, err := job.gitHeadCommit(job.artifactsRepoDir)
	if err != nil {
		t.Fatalf("resolve cloned HEAD: %v", err)
	}
	if headCommit != secondCommit {
		t.Fatalf("expected cloned HEAD %q, got %q", secondCommit, headCommit)
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

func TestSubmodulesEnabledDefaultsToTrue(t *testing.T) {
	t.Parallel()

	job := newNativeBuildJob(context.Background(), newTestMacBuild(), ArtifactsScriptSourceConfig{})
	if !job.submodulesEnabled() {
		t.Fatal("expected submodules to be initialized by default (Tekton pingcap-git-clone parity)")
	}

	disabled := false
	macBuild := newTestMacBuild()
	macBuild.Spec.Source.Submodules = &disabled
	job = newNativeBuildJob(context.Background(), macBuild, ArtifactsScriptSourceConfig{})
	if job.submodulesEnabled() {
		t.Fatal("expected source.submodules=false to skip submodule initialization")
	}

	enabled := true
	macBuild.Spec.Source.Submodules = &enabled
	job = newNativeBuildJob(context.Background(), macBuild, ArtifactsScriptSourceConfig{})
	if !job.submodulesEnabled() {
		t.Fatal("expected source.submodules=true to initialize submodules")
	}
}

func TestCloneAndCheckoutSourceInitializesSubmodules(t *testing.T) {
	// Local-path submodules need the file protocol allowed (git >= 2.38).
	t.Setenv("GIT_ALLOW_PROTOCOL", "file")

	superDir, commit := createRepoWithSubmoduleFixture(t)
	macBuild := newTestMacBuild()
	macBuild.Spec.Source = buildv1alpha1.SourceSpec{GitRepository: superDir, GitRef: "main"}

	job := newNativeBuildJob(context.Background(), macBuild, ArtifactsScriptSourceConfig{})
	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	defer job.cleanup()

	gotCommit, err := job.cloneAndCheckoutSource()
	if err != nil {
		t.Fatalf("clone and checkout: %v", err)
	}
	if gotCommit != commit {
		t.Fatalf("expected commit %q, got %q", commit, gotCommit)
	}

	subFile := filepath.Join(job.sourceDir, macBuild.Spec.Build.Component, "sub", "sub.txt")
	if _, err := os.Stat(subFile); err != nil {
		t.Fatalf("expected submodule file %s to be checked out: %v", subFile, err)
	}
}

func TestCloneAndCheckoutSourceSkipsSubmodulesWhenDisabled(t *testing.T) {
	t.Setenv("GIT_ALLOW_PROTOCOL", "file")

	superDir, _ := createRepoWithSubmoduleFixture(t)
	disabled := false
	macBuild := newTestMacBuild()
	macBuild.Spec.Source = buildv1alpha1.SourceSpec{
		GitRepository: superDir,
		GitRef:        "main",
		Submodules:    &disabled,
	}

	job := newNativeBuildJob(context.Background(), macBuild, ArtifactsScriptSourceConfig{})
	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	defer job.cleanup()

	if _, err := job.cloneAndCheckoutSource(); err != nil {
		t.Fatalf("clone and checkout: %v", err)
	}

	subFile := filepath.Join(job.sourceDir, macBuild.Spec.Build.Component, "sub", "sub.txt")
	if _, err := os.Stat(subFile); !os.IsNotExist(err) {
		t.Fatalf("expected submodule content to be absent when submodules are disabled, stat err: %v", err)
	}
}

func TestGitCommandAppliesCredentials(t *testing.T) {
	t.Parallel()

	job := newNativeBuildJob(context.Background(), newTestMacBuild(), ArtifactsScriptSourceConfig{})

	creds := &gitCredentials{
		kind: gitCredentialKindBasicAuth,
		args: []string{"-c", "credential.helper="},
		env:  []string{"GIT_TERMINAL_PROMPT=0"},
	}
	cmd := job.gitCommand(creds, "clone", "https://example.invalid/repo.git", "/tmp/x")
	if got := strings.Join(cmd.Args, " "); !strings.Contains(got, "git -c credential.helper= clone") {
		t.Fatalf("expected credential overrides ahead of the git subcommand, got %q", got)
	}
	found := false
	for _, entry := range cmd.Env {
		if entry == "GIT_TERMINAL_PROMPT=0" {
			found = true
		}
	}
	if !found {
		t.Fatal("expected credential environment to be applied")
	}

	// Anonymous clones keep the inherited environment untouched.
	plain := job.gitCommand(nil, "status")
	if plain.Env != nil {
		t.Fatalf("expected no environment override without credentials, got %v", plain.Env)
	}
}

func TestCloneAndCheckoutSourceWithRefspecAndGitSha(t *testing.T) {
	t.Parallel()

	repoDir := t.TempDir()
	runGit(t, repoDir, "init", "-b", "main")
	runGit(t, repoDir, "config", "user.name", "Tester")
	runGit(t, repoDir, "config", "user.email", "tester@example.com")
	if err := os.WriteFile(filepath.Join(repoDir, "README.md"), []byte("repo\n"), 0o644); err != nil {
		t.Fatalf("write repo file: %v", err)
	}
	runGit(t, repoDir, "add", ".")
	runGit(t, repoDir, "commit", "-m", "initial")
	runGit(t, repoDir, "branch", "release/1.0")

	refspec := "+refs/heads/release/*:refs/remotes/origin/release/*"
	sha := strings.TrimSpace(runGit(t, repoDir, "rev-parse", "release/1.0"))

	macBuild := newTestMacBuild()
	macBuild.Spec.Source = buildv1alpha1.SourceSpec{
		GitRepository: repoDir,
		GitRef:        "main",
		GitSha:        &sha,
		GitRefspec:    &refspec,
	}

	job := newNativeBuildJob(context.Background(), macBuild, ArtifactsScriptSourceConfig{})
	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	defer job.cleanup()

	gotCommit, err := job.cloneAndCheckoutSource()
	if err != nil {
		t.Fatalf("clone and checkout: %v", err)
	}
	if gotCommit != sha {
		t.Fatalf("expected gitSha %q to be checked out, got %q", sha, gotCommit)
	}
}

func TestCloneAndCheckoutSourceRemovesMaterializedCredentialsOnFailure(t *testing.T) {
	t.Parallel()

	// A local path cannot host basic-auth credentials: resolving fails before the
	// clone. The materialized credentials directory must not be left behind.
	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "/tmp/local-repo",
		GitRef:        "main",
		GitSecretRef:  stringPtr(testCredentialsSecretName),
	}, basicAuthSecret(testCredentialsSecretName, testCredentialsToken))

	if _, err := job.cloneAndCheckoutSource(); err == nil {
		t.Fatal("expected cloneAndCheckoutSource to fail for local paths with basic-auth credentials")
	}

	if _, err := os.Stat(filepath.Join(job.workspaceDir, gitCredentialsDirName)); !os.IsNotExist(err) {
		t.Fatalf("expected credentials dir to be cleaned up, stat err: %v", err)
	}
}

func createRepoWithSubmoduleFixture(t *testing.T) (superDir, commit string) {
	t.Helper()

	subDir := t.TempDir()
	runGit(t, subDir, "init", "-b", "main")
	runGit(t, subDir, "config", "user.name", "Tester")
	runGit(t, subDir, "config", "user.email", "tester@example.com")
	if err := os.WriteFile(filepath.Join(subDir, "sub.txt"), []byte("from-submodule\n"), 0o644); err != nil {
		t.Fatalf("write submodule file: %v", err)
	}
	runGit(t, subDir, "add", ".")
	runGit(t, subDir, "commit", "-m", "sub initial")

	superDir = t.TempDir()
	runGit(t, superDir, "init", "-b", "main")
	runGit(t, superDir, "config", "user.name", "Tester")
	runGit(t, superDir, "config", "user.email", "tester@example.com")
	if err := os.WriteFile(filepath.Join(superDir, "README.md"), []byte("super\n"), 0o644); err != nil {
		t.Fatalf("write superproject file: %v", err)
	}
	runGit(t, superDir, "add", ".")
	runGit(t, superDir, "commit", "-m", "super initial")

	runGitEnv(t, superDir, []string{"GIT_ALLOW_PROTOCOL=file"}, "submodule", "add", subDir, "sub")
	runGit(t, superDir, "commit", "-m", "add submodule")

	return superDir, strings.TrimSpace(runGit(t, superDir, "rev-parse", "HEAD"))
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

	return runGitEnv(t, dir, nil, args...)
}

func runGitEnv(t *testing.T, dir string, env []string, args ...string) string {
	t.Helper()

	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	if len(env) > 0 {
		cmd.Env = append(os.Environ(), env...)
	}
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, output)
	}
	return string(output)
}
