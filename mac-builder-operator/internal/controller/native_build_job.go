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
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/go-logr/logr"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

// nativeBuildJob represents a build job for native Mac builds.
type nativeBuildJob struct {
	ctx      context.Context
	logger   logr.Logger
	macBuild buildv1alpha1.MacBuild
	spec     buildv1alpha1.MacBuildSpec // shortcut

	// Paths
	workspaceDir     string
	sourceDir        string
	artifactsRepoDir string
	buildScriptPath  string
	envFilePath      string
	pushedResultPath string
}

// newNativeBuildJob creates a new instance of nativeBuildJob.
func newNativeBuildJob(ctx context.Context, macBuild buildv1alpha1.MacBuild) *nativeBuildJob {
	logger := logf.FromContext(ctx)

	// Create in 'os.TempDir()' (e.g., /var/folders/...)
	// 'workspaceDir' will be created in 'setupWorkspace'
	baseDir := os.TempDir()
	workspaceName := fmt.Sprintf("macbuild-%s-%d", macBuild.Name, time.Now().UnixNano())
	workspaceDir := filepath.Join(baseDir, workspaceName)

	return &nativeBuildJob{
		ctx:      ctx,
		logger:   logger.WithValues("job", macBuild.Namespace, "workspace", workspaceDir),
		macBuild: macBuild,
		spec:     macBuild.Spec,

		workspaceDir:     workspaceDir,
		sourceDir:        filepath.Join(workspaceDir, "source"),
		artifactsRepoDir: filepath.Join(workspaceDir, "artifacts"),
		buildScriptPath:  filepath.Join(workspaceDir, "build-package-artifacts.sh"),
		envFilePath:      filepath.Join(workspaceDir, "remote.env"),
		pushedResultPath: filepath.Join(workspaceDir, "pushed.yaml"),
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

	if err := j.generateBuildScript(); err != nil {
		return result, err
	}

	if _, err := os.Stat(j.buildScriptPath); os.IsNotExist(err) {
		j.logger.Info("Build script was not generated, skipping build. (This may be expected for some components)")
		return result, nil
	}

	if err := j.executeBuild(); err != nil {
		return result, err
	}
	if !j.spec.Artifacts.Push {
		j.logger.Info("spec.artifacts.push is false, skipping publish phase.")
		return result, nil
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
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if len(dir) > 0 {
		cmd.Dir = dir[0]
	}

	j.logger.Info("Executing command", "cmd", cmd.String(), "dir", cmd.Dir)

	if err := cmd.Run(); err != nil {
		stderrStr := stderr.String()
		j.logger.Error(err, "Command execution failed", "stderr", stderrStr)
		// Return stderr as the error message; this is useful
		return errors.New(stderrStr)
	}

	j.logger.Info("Command executed successfully", "stdout", stdout.String())
	return nil
}

// cloneArtifactsRepo clones the artifacts repository.
func (j *nativeBuildJob) cloneArtifactsRepo() error {
	j.logger.Info("Cloning artifacts repository...")
	artifactsRepoURL := "https://github.com/PingCAP-QE/artifacts.git"
	cmd := exec.Command("git", "clone", "--depth=1", "--branch=main", artifactsRepoURL, j.artifactsRepoDir)
	if err := j.exec(cmd); err != nil {
		return fmt.Errorf("failed to clone artifacts repo: %w", err)
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

// generateEnvFile generates the environment file for the build.
func (j *nativeBuildJob) generateEnvFile() error {
	j.logger.Info("Generating environment file...")

	goVerCmd := exec.Command("go", "version")
	goVerOut, err := goVerCmd.Output()
	var goBinPath string
	if err == nil {
		parts := strings.Split(string(goVerOut), " ")
		if len(parts) >= 3 {
			verParts := strings.Split(parts[2], ".")
			if len(verParts) >= 2 {
				goBinPath = fmt.Sprintf("/usr/local/%s.%s/bin", verParts[0], verParts[1])
			}
		}
	} else {
		j.logger.Error(err, "Failed to get 'go version', $PATH may be incomplete in env file")
	}

	envContent := fmt.Sprintf(`
export LC_ALL=C.UTF-8
export PATH=%s:$PATH
export NPM_CONFIG_REGISTRY="https://registry.npmmirror.com"
export NODE_OPTIONS="--max_old_space_size=8192"
export CARGO_NET_GIT_FETCH_WITH_CLI=true
`, goBinPath)

	if err := os.WriteFile(j.envFilePath, []byte(envContent), 0644); err != nil {
		return fmt.Errorf("failed to write env file: %w", err)
	}
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
	if err := j.exec(cmdGenScript); err != nil {
		return fmt.Errorf("failed to generate build script: %w", err)
	}
	return nil
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

	cmdBuild := exec.Command(runScriptPath)
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

	cmdPublish := exec.Command(j.buildScriptPath, "-p", "-w", releaseDir, "-o", j.pushedResultPath)
	buildDir := filepath.Join(j.sourceDir, j.spec.Build.Component)

	if err := j.exec(cmdPublish, buildDir); err != nil {
		j.logger.Info("Publish failed, retrying once...", "error", err)
		if errRetry := j.exec(cmdPublish, buildDir); errRetry != nil {
			return "", fmt.Errorf("publish execution failed after retry: %w", errRetry)
		}
	}

	j.logger.Info("Publish complete, reading results YAML.")
	pushedYAMLBytes, err := os.ReadFile(j.pushedResultPath)
	if err != nil {
		return "", fmt.Errorf("failed to read pushed result file: %w", err)
	}

	return string(pushedYAMLBytes), nil
}
