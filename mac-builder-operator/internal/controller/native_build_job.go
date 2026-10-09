/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/go-logr/logr"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

// nativeBuildJob represents a build job for native Mac builds.
type nativeBuildJob struct {
	ctx                   context.Context
	logger                logr.Logger
	macBuild              buildv1alpha1.MacBuild
	spec                  buildv1alpha1.MacBuildSpec // shortcut
	artifactsScriptSource ArtifactsScriptSourceConfig

	// Paths
	workspaceDir         string
	sourceDir            string
	artifactsRepoDir     string
	buildScriptPath      string
	envFilePath          string
	pushedResultPath     string
	releasePackagePath   string
	miseConfigPath       string
	toolchainProvisioned bool

	reportPhase  buildPhaseReporter
	stdoutWriter io.Writer
	stderrWriter io.Writer
}

// newNativeBuildJob creates a new instance of nativeBuildJob.
func newNativeBuildJob(
	ctx context.Context,
	macBuild buildv1alpha1.MacBuild,
	artifactsScriptSource ArtifactsScriptSourceConfig,
) *nativeBuildJob {
	logger := logf.FromContext(ctx)

	// Create in 'os.TempDir()' (e.g., /var/folders/...)
	// 'workspaceDir' will be created in 'setupWorkspace'
	baseDir := os.TempDir()
	workspaceName := fmt.Sprintf("macbuild-%s-%d", macBuild.Name, time.Now().UnixNano())
	workspaceDir := filepath.Join(baseDir, workspaceName)

	return &nativeBuildJob{
		ctx:                   ctx,
		logger:                logger.WithValues("job", macBuild.Namespace, "workspace", workspaceDir),
		macBuild:              macBuild,
		spec:                  macBuild.Spec,
		artifactsScriptSource: artifactsScriptSource,

		workspaceDir:     workspaceDir,
		sourceDir:        filepath.Join(workspaceDir, "source"),
		artifactsRepoDir: filepath.Join(workspaceDir, "artifacts"),
		buildScriptPath:  filepath.Join(workspaceDir, "build-package-artifacts.sh"),
		envFilePath:      filepath.Join(workspaceDir, "remote.env"),
		pushedResultPath: filepath.Join(workspaceDir, "pushed.yaml"),
		// release-package.yaml is written by the artifacts gen script (see
		// generateBuildScript) and carries the matched builder's macos.tools.
		releasePackagePath: filepath.Join(workspaceDir, "release-package.yaml"),
		miseConfigPath:     filepath.Join(workspaceDir, "mise.toml"),
		stdoutWriter:       os.Stdout,
		stderrWriter:       os.Stderr,
	}
}

func (j *nativeBuildJob) Run() (*buildResult, error) {
	// steps:
	// 1. setup workspace
	// 2. clone the source
	// 3. generate build script
	// 4. run build script
	// 5. push the binary artifacts

	if err := j.setupWorkspace(); err != nil {
		return nil, err
	}
	defer j.cleanup()

	if err := j.cloneArtifactsRepo(); err != nil {
		return nil, err
	}

	commitHash, err := j.cloneAndCheckoutSource()
	if err != nil {
		return nil, err
	}
	result := &buildResult{CommitHash: commitHash}

	if err := j.generateEnvFile(); err != nil {
		return result, err
	}

	if err := j.resolveBuildVersion(); err != nil {
		return result, err
	}

	if err := j.generateBuildScript(); err != nil {
		return result, err
	}

	if _, err := os.Stat(j.buildScriptPath); os.IsNotExist(err) {
		j.logger.Info("Build script was not generated, skipping build. (This may be expected for some components)")
		return result, nil
	}

	if err := j.provisionToolchain(); err != nil {
		return result, err
	}

	if err := j.updatePhase(buildv1alpha1.PhaseBuilding, "Running build steps on the worker."); err != nil {
		return result, err
	}
	if err := j.executeBuild(); err != nil {
		return result, err
	}
	if !j.spec.Artifacts.Push {
		j.logger.Info("spec.artifacts.push is false, skipping publish phase.")
		return result, nil
	}

	if err := j.updatePhase(buildv1alpha1.PhasePublishing, "Publishing build artifacts."); err != nil {
		return result, err
	}
	pushedYAML, err := j.executePublish()
	if err != nil {
		return result, err
	}

	result.PushedArtifactsYaml = pushedYAML
	return result, nil
}

// setupWorkspace creates the workspace directory for the build job.
func (j *nativeBuildJob) setupWorkspace() error {
	j.logger.Info("Creating workspace", "dir", j.workspaceDir)
	if err := os.MkdirAll(j.workspaceDir, 0755); err != nil {
		return fmt.Errorf("failed to create temp workspace: %w", err)
	}
	return nil
}

// cleanup removes the workspace directory and its contents.
func (j *nativeBuildJob) cleanup() {
	j.logger.Info("Cleaning up workspace", "dir", j.workspaceDir)
	if err := os.RemoveAll(j.workspaceDir); err != nil {
		j.logger.Error(err, "Failed to clean up workspace")
	}
}

// exec executes a command and logs its output.
func (j *nativeBuildJob) exec(cmd *exec.Cmd, dir ...string) error {
	if len(dir) > 0 {
		cmd.Dir = dir[0]
	}

	stdoutWriter := j.stdoutWriter
	if stdoutWriter == nil {
		stdoutWriter = io.Discard
	}
	stderrWriter := j.stderrWriter
	if stderrWriter == nil {
		stderrWriter = io.Discard
	}
	tail := newTailBuffer(8 * 1024)
	cmd.Stdout = io.MultiWriter(stdoutWriter, tail)
	cmd.Stderr = io.MultiWriter(stderrWriter, tail)

	j.logger.Info("Executing command", "cmd", cmd.String(), "dir", cmd.Dir)

	if err := cmd.Run(); err != nil {
		tailSummary := strings.TrimSpace(tail.String())
		if tailSummary != "" {
			j.logger.Error(err, "Command execution failed", "tail", tailSummary)
			return fmt.Errorf("command execution failed: %s", tailSummary)
		}
		j.logger.Error(err, "Command execution failed")
		return fmt.Errorf("command execution failed: %w", err)
	}

	j.logger.Info("Command executed successfully", "cmd", cmd.String())
	return nil
}

// cloneArtifactsRepo clones the artifacts repository.
func (j *nativeBuildJob) cloneArtifactsRepo() error {
	source, err := j.artifactsScriptSource.Normalize()
	if err != nil {
		return fmt.Errorf("invalid artifacts repo source: %w", err)
	}

	j.logger.Info(
		"Cloning artifacts repository...",
		"repo", source.URL,
		"revision", source.Revision,
		"expectedCommit", source.ExpectedCommit,
	)

	cmd := exec.Command("git", "clone", "--no-checkout", "--filter=blob:none", source.URL, j.artifactsRepoDir)
	if err := j.exec(cmd); err != nil {
		return fmt.Errorf("failed to clone artifacts repo: %w", err)
	}

	checkoutRef, err := j.resolveArtifactsCheckoutRef(source.Revision)
	if err != nil {
		return err
	}

	cmd = exec.Command("git", "checkout", checkoutRef)
	if err := j.exec(cmd, j.artifactsRepoDir); err != nil {
		return fmt.Errorf("failed to checkout artifacts repo revision %q: %w", source.Revision, err)
	}

	headCommit, err := j.gitHeadCommit(j.artifactsRepoDir)
	if err != nil {
		return fmt.Errorf("failed to resolve artifacts repo HEAD commit: %w", err)
	}
	if source.ExpectedCommit != "" && headCommit != source.ExpectedCommit {
		return fmt.Errorf(
			"artifacts repo revision %q resolved to %q, expected %q",
			source.Revision,
			headCommit,
			source.ExpectedCommit,
		)
	}

	return nil
}

// cloneAndCheckoutSource clones the source repository and checks out the specified ref.
func (j *nativeBuildJob) cloneAndCheckoutSource() (string, error) {
	j.logger.Info("Cloning source repository", "repo", j.spec.Source.GitRepository)

	toCloneDir := filepath.Join(j.sourceDir, j.spec.Build.Component)
	cmdClone := exec.Command("git", "clone", j.spec.Source.GitRepository, toCloneDir)
	if err := j.exec(cmdClone); err != nil {
		return "", fmt.Errorf("failed to clone source repo: %w", err)
	}

	if j.spec.Source.GitRefspec != nil && *j.spec.Source.GitRefspec != "" {
		j.logger.Info("Fetching refspec", "refspec", *j.spec.Source.GitRefspec)
		cmdFetch := exec.Command("git", "fetch", "origin", *j.spec.Source.GitRefspec)
		if err := j.exec(cmdFetch, toCloneDir); err != nil {
			return "", fmt.Errorf("failed to fetch refspec: %w", err)
		}
	}

	checkoutRef := j.spec.Source.GitRef
	if j.spec.Source.GitSha != nil && *j.spec.Source.GitSha != "" {
		checkoutRef = *j.spec.Source.GitSha
	}
	j.logger.Info("Checking out source", "ref", checkoutRef)
	cmdCheckout := exec.Command("git", "checkout", checkoutRef)
	if err := j.exec(cmdCheckout, toCloneDir); err != nil {
		return "", fmt.Errorf("failed to checkout source: %w", err)
	}

	// Get the final commit hash
	cmdHash := exec.Command("git", "rev-parse", "HEAD")
	cmdHash.Dir = toCloneDir
	hashBytes, err := cmdHash.Output() // Output() bypasses j.exec
	if err != nil {
		return "", fmt.Errorf("failed to get commit hash: %w", err)
	}
	commitHash := strings.TrimSpace(string(hashBytes))
	j.logger.Info("Source checked out", "commitHash", commitHash)
	return commitHash, nil
}

func (j *nativeBuildJob) gitHeadCommit(dir string) (string, error) {
	cmdHash := exec.Command("git", "rev-parse", "HEAD")
	cmdHash.Dir = dir
	hashBytes, err := cmdHash.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(hashBytes)), nil
}

// resolveArtifactsCheckoutRef returns the ref git should check out. The revision
// may be a branch (e.g. main), a tag, or a full commit SHA; git resolves all.
func (j *nativeBuildJob) resolveArtifactsCheckoutRef(revision string) (string, error) {
	return revision, nil
}

// generateEnvFile generates the environment file for the build. Go is provided
// per-build via mise (see provisionToolchain), so PATH is intentionally not
// rewritten here — prepending a system go bin would shadow mise's toolchain and
// cause "compile: version ... does not match go tool version ..." mismatches.
func (j *nativeBuildJob) generateEnvFile() error {
	j.logger.Info("Generating environment file...")

	envContent := `
export LC_ALL=C.UTF-8
export NPM_CONFIG_REGISTRY="https://registry.npmmirror.com"
export NODE_OPTIONS="--max_old_space_size=8192"
export CARGO_NET_GIT_FETCH_WITH_CLI=true
`

	if err := os.WriteFile(j.envFilePath, []byte(envContent), 0644); err != nil {
		return fmt.Errorf("failed to write env file: %w", err)
	}
	return nil
}

// defaultVersioningStrategyURL mirrors the deno script used by the
// pingcap-get-set-release-version Tekton task.
const defaultVersioningStrategyURL = "https://cdn.jsdelivr.net/gh/PingCAP-QE/ci@main/scripts/flow/build/versioning-strategy.ts"

// resolveBuildVersion computes the release version from the checked-out source
// when spec.build.version is empty. Darwin builds no longer run a Tekton
// get-release-ver task, so the agent resolves the version itself (mirroring
// pingcap-get-set-release-version's steps).
func (j *nativeBuildJob) resolveBuildVersion() error {
	if strings.TrimSpace(j.spec.Build.Version) != "" {
		return nil
	}

	sourceDir := filepath.Join(j.sourceDir, j.spec.Build.Component)
	rawVersionPath := filepath.Join(j.workspaceDir, "raw-version.txt")
	branchesPath := filepath.Join(j.workspaceDir, "branches.txt")
	newTagPath := filepath.Join(j.workspaceDir, "new-tag")
	versionPath := filepath.Join(j.workspaceDir, "resolved-version.txt")

	j.logger.Info("Resolving build version from source...")

	prepareScript := fmt.Sprintf(`set -e
if git tag | grep -E "v[0-9]+[.][0-9]+[.][0-9]+(-(alpha|beta|rc|release|nextgen|fips|cse)([.0-9]+)?)?$" > /dev/null; then
  git tag | grep -vE "^v[0-9]+[.][0-9]+[.][0-9]+(-(((alpha|beta|rc|release|nextgen)([.].+)?)|fips|cse|202[1-9][0-1][0-9][0-3][0-9]-[0-9a-f]{7,10}))?$" | xargs git tag -d || true
fi
git tag | grep -E "^v20[0-9][0-9].[0-1]{1,2}.[0-3][0-9]" | xargs git tag -d || true
git describe --tags --always --dirty > %q
git branch --contains > %q
`, rawVersionPath, branchesPath)
	if err := j.exec(exec.Command("sh", "-c", prepareScript), sourceDir); err != nil {
		return fmt.Errorf("failed to prepare version inputs: %w", err)
	}

	denoCmd := exec.Command("deno", "run", "--allow-read", "--allow-write", defaultVersioningStrategyURL,
		"--git_version_file="+rawVersionPath,
		"--contain_branches_file="+branchesPath,
		"--save_build_git_tag_file="+newTagPath,
		"--save_release_version_file="+versionPath,
	)
	if err := j.exec(denoCmd); err != nil {
		return fmt.Errorf("failed to compute version: %w", err)
	}

	applyScript := fmt.Sprintf(`set -e
if [ -f %q ]; then
  NEW_TAG=$(cat %q)
  git tag --contains | xargs git tag -d
  git tag -f "$NEW_TAG"
fi
`, newTagPath, newTagPath)
	if err := j.exec(exec.Command("sh", "-c", applyScript), sourceDir); err != nil {
		return fmt.Errorf("failed to apply version tag: %w", err)
	}

	versionBytes, err := os.ReadFile(versionPath)
	if err != nil {
		return fmt.Errorf("failed to read resolved version: %w", err)
	}
	j.spec.Build.Version = strings.TrimSpace(string(versionBytes))
	if j.spec.Build.Version == "" {
		return fmt.Errorf("resolved an empty build version")
	}
	j.logger.Info("Resolved build version", "version", j.spec.Build.Version)
	return nil
}

// generateBuildScript generates the build script for the build job.
func (j *nativeBuildJob) generateBuildScript() error {
	j.logger.Info("Generating build script...")
	genScript := filepath.Join(j.artifactsRepoDir, "packages/scripts/gen-package-artifacts-with-config.sh")

	gitSha := ""
	if j.spec.Source.GitSha != nil {
		gitSha = *j.spec.Source.GitSha
	}
	if j.spec.Source.GitRef == gitSha {
		gitSha = ""
	}

	cmdGenScript := exec.Command(genScript,
		j.spec.Build.Component,
		"darwin", // OS
		j.spec.Build.Arch,
		j.spec.Build.Version,
		j.spec.Build.Profile,
		j.spec.Source.GitRef,
		gitSha,
		filepath.Join(j.artifactsRepoDir, "packages/packages.yaml.tmpl"),
		j.buildScriptPath,
		j.spec.Artifacts.Registry,
	)
	if err := j.exec(cmdGenScript, j.workspaceDir); err != nil {
		return fmt.Errorf("failed to generate build script: %w", err)
	}
	return nil
}

// provisionToolchain resolves the component's declared macos.tools (from the
// rendered release-package.yaml) and provisions them per build via mise: it
// writes a per-build mise.toml and runs `mise install` (isolated config, shared
// tool cache). When no macos.tools is declared it leaves the job on the ambient
// (bootstrap/global) toolchain — the fallback path.
func (j *nativeBuildJob) provisionToolchain() error {
	data, err := os.ReadFile(j.releasePackagePath)
	if err != nil {
		if os.IsNotExist(err) {
			j.logger.Info("No rendered component config; using ambient toolchain.")
			return nil
		}
		return fmt.Errorf("read component config: %w", err)
	}

	tools, err := resolveMacOSTools(data)
	if err != nil {
		return err
	}
	if len(tools) == 0 {
		j.logger.Info("No macos.tools declared for this component/version; using ambient toolchain.")
		return nil
	}

	miseToml, err := renderMiseToml(tools)
	if err != nil {
		return err
	}
	if err := os.WriteFile(j.miseConfigPath, []byte(miseToml), 0o644); err != nil {
		return fmt.Errorf("write mise.toml: %w", err)
	}

	j.logger.Info("Provisioning toolchain via mise", "tools", tools)
	if err := j.exec(j.miseCommand("install"), j.workspaceDir); err != nil {
		return fmt.Errorf("mise install: %w", err)
	}
	j.toolchainProvisioned = true
	return nil
}

// miseCommand builds a `mise ...` command bound to this build's mise.toml.
func (j *nativeBuildJob) miseCommand(args ...string) *exec.Cmd {
	cmd := exec.Command("mise", args...)
	cmd.Env = append(os.Environ(), "MISE_CONFIG_FILE="+j.miseConfigPath)
	return cmd
}

// toolchainCommand runs name with args. When a per-build toolchain was
// provisioned it executes under `mise exec` (so the declared tool versions are
// used); otherwise it runs the command directly (ambient/fallback toolchain).
func (j *nativeBuildJob) toolchainCommand(name string, args ...string) *exec.Cmd {
	if j.toolchainProvisioned {
		return j.miseCommand(append([]string{"exec", "--", name}, args...)...)
	}
	return exec.Command(name, args...)
}

// createRunnableScript creates a runnable script with the given content.
func (j *nativeBuildJob) createRunnableScript(wrapperName string, scriptContent string) (string, error) {
	scriptPath := filepath.Join(j.workspaceDir, wrapperName)
	fullContent := fmt.Sprintf("#!/bin/bash\nset -eo pipefail\n%s\n", scriptContent)

	if err := os.WriteFile(scriptPath, []byte(fullContent), 0755); err != nil {
		return "", fmt.Errorf("failed to create runnable script %s: %w", wrapperName, err)
	}
	return scriptPath, nil
}

// executeBuild executes the build script.
func (j *nativeBuildJob) executeBuild() error {
	j.logger.Info("Executing build script (Build phase)...")
	releaseDir := filepath.Join(j.sourceDir, j.spec.Build.Component, "build")

	scriptContent := fmt.Sprintf(`source %s;%s -b -a -w %s`, j.envFilePath, j.buildScriptPath, releaseDir)
	runScriptPath, err := j.createRunnableScript("run_build.sh", scriptContent)
	if err != nil {
		return err
	}

	cmdBuild := j.toolchainCommand(runScriptPath)
	buildDir := filepath.Join(j.sourceDir, j.spec.Build.Component)
	if err := j.exec(cmdBuild, buildDir); err != nil {
		return fmt.Errorf("build execution failed: %w", err)
	}
	return nil
}

// executePublish executes the publish phase of the build script.
func (j *nativeBuildJob) executePublish() (string, error) {
	j.logger.Info("Executing build script (Publish phase)...")
	releaseDir := filepath.Join(j.sourceDir, j.spec.Build.Component, "build")

	buildDir := filepath.Join(j.sourceDir, j.spec.Build.Component)

	// Build a fresh command per attempt: an *exec.Cmd can only be started once.
	cmdPublish := j.toolchainCommand(j.buildScriptPath, "-p", "-w", releaseDir, "-o", j.pushedResultPath)
	if err := j.exec(cmdPublish, buildDir); err != nil {
		firstErr := err
		j.logger.Info("Publish failed, retrying once...", "error", firstErr)
		retry := j.toolchainCommand(j.buildScriptPath, "-p", "-w", releaseDir, "-o", j.pushedResultPath)
		if errRetry := j.exec(retry, buildDir); errRetry != nil {
			return "", fmt.Errorf("publish execution failed: %w (first attempt: %v)", errRetry, firstErr)
		}
	}

	j.logger.Info("Publish complete, reading results YAML.")
	pushedYAMLBytes, err := os.ReadFile(j.pushedResultPath)
	if err != nil {
		return "", fmt.Errorf("failed to read pushed result file: %w", err)
	}

	return string(pushedYAMLBytes), nil
}

func (j *nativeBuildJob) updatePhase(phase string, message string) error {
	if j.reportPhase == nil {
		return nil
	}
	return j.reportPhase(phase, message)
}

type tailBuffer struct {
	mu        sync.Mutex
	buf       []byte
	maxBytes  int
	truncated bool
}

func newTailBuffer(maxBytes int) *tailBuffer {
	return &tailBuffer{maxBytes: maxBytes}
}

func (b *tailBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.buf = append(b.buf, p...)
	if len(b.buf) > b.maxBytes {
		b.truncated = true
		b.buf = append([]byte(nil), b.buf[len(b.buf)-b.maxBytes:]...)
	}

	return len(p), nil
}

func (b *tailBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()

	if len(b.buf) == 0 {
		return ""
	}
	if b.truncated {
		return "...\n" + string(b.buf)
	}
	return string(b.buf)
}
