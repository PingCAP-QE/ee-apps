/*
Copyright 2025.

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

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/go-logr/logr"
)

// MacBuildReconciler reconciles a MacBuild object
type MacBuildReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	WorkerID string
}

type buildResult struct {
	CommitHash          string
	PushedArtifactsYaml string
}

// +kubebuilder:rbac:groups=build.tibuild.pingcap.net,resources=macbuilds,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=build.tibuild.pingcap.net,resources=macbuilds/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=build.tibuild.pingcap.net,resources=macbuilds/finalizers,verbs=update

// Reconcile is part of the main kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state.
// TODO(user): Modify the Reconcile function to compare the state specified by
// the MacBuild object against the actual cluster state, and then
// perform operations to make the cluster state reflect the state specified by
// the user.
//
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.22.1/pkg/reconcile
func (r *MacBuildReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := logf.FromContext(ctx)
	logger.Info("Reconciling MacBuild...")

	// get macBuild object
	var macBuild buildv1alpha1.MacBuild
	if err := r.Get(ctx, req.NamespacedName, &macBuild); err != nil {
		if apierrors.IsNotFound(err) {
			logger.Info("MacBuild resource not found. Ignoring since object must be deleted.")
			return ctrl.Result{}, nil
		}
		logger.Error(err, "Failed to get MacBuild")
		return ctrl.Result{}, err
	}

	// init the status for the object
	if macBuild.Status.Phase == "" {
		logger.Info("Setting initial status to Pending")

		newStatus := macBuild.Status.DeepCopy()
		newStatus.Phase = buildv1alpha1.PhasePending
		macBuild.Status = *newStatus

		if err := r.Status().Update(ctx, &macBuild); err != nil {
			logger.Error(err, "Failed to update MacBuild status to Pending")
			return ctrl.Result{}, err
		}

		return ctrl.Result{Requeue: true}, nil
	}

	logger.Info("MacBuild already has status", "Phase", macBuild.Status.Phase)
	switch macBuild.Status.Phase {
	case buildv1alpha1.PhasePending:
		// adopt the build job and update the status to "Building"
		logger.Info("Phase: Pending. Claiming and setting to Building.")
		newStatus := macBuild.Status.DeepCopy()
		newStatus.Phase = buildv1alpha1.PhaseBuilding
		newStatus.WorkerID = &r.WorkerID
		now := metav1.Now()
		newStatus.StartTime = &now
		macBuild.Status = *newStatus

		if err := r.Status().Update(ctx, &macBuild); err != nil {
			logger.Error(err, "Failed to update status to Building")
			return ctrl.Result{}, err
		}

		return ctrl.Result{Requeue: true}, nil
	case buildv1alpha1.PhaseBuilding:
		logger.Info("Phase: Building. Starting build process.")

		if macBuild.Status.WorkerID == nil || *macBuild.Status.WorkerID != r.WorkerID {
			logger.Info("This build is not assigned to me.", "AssignedWorker", macBuild.Status.WorkerID)
			return ctrl.Result{}, nil
		}

		// check the run status.
		// TODO: in production, we need a more complex logic to check the long time goroutine.
		result, err := r.runNativeBuild(ctx, macBuild)
		newStatus := macBuild.Status.DeepCopy()
		now := metav1.Now()
		newStatus.CompletionTime = &now

		if err != nil {
			logger.Error(err, "Build failed")
			newStatus.Phase = buildv1alpha1.PhaseFailed
			errMsg := err.Error()
			newStatus.Message = &errMsg
		} else {
			logger.Info("Build succeeded")
			newStatus.Phase = buildv1alpha1.PhaseSucceeded
			newStatus.CommitHash = &result.CommitHash
			if result.PushedArtifactsYaml != "" {
				newStatus.Outputs.PushedArtifactsYaml = &result.PushedArtifactsYaml
			}
		}

		macBuild.Status = *newStatus
		if errUpdate := r.Status().Update(ctx, &macBuild); errUpdate != nil {
			logger.Error(errUpdate, "Failed to update status to Succeeded/Failed")
			return ctrl.Result{}, errUpdate
		}
		return ctrl.Result{}, nil
	case buildv1alpha1.PhaseSucceeded, buildv1alpha1.PhaseFailed:
		logger.WithValues("phase", macBuild.Status.Phase).Info("Nothing to do.")
		return ctrl.Result{}, nil
	default:
		logger.Info("Unknown phase, ignoring", "Phase", macBuild.Status.Phase)
		return ctrl.Result{}, nil
	}
}

// SetupWithManager sets up the controller with the Manager.
func (r *MacBuildReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&buildv1alpha1.MacBuild{}).
		Named("macbuild").
		Complete(r)
}

func (r *MacBuildReconciler) runNativeBuild(ctx context.Context, macBuild buildv1alpha1.MacBuild) (*buildResult, error) {
	job := newNativeBuildJob(r, ctx, macBuild)

	// steps:
	// 1. setup workspace
	// 2. clone the source
	// 3. generate build script
	// 4. run build script
	// 5. push the binary artifacts

	if err := job.setupWorkspace(); err != nil {
		return nil, err
	}
	defer job.cleanup()

	if err := job.cloneArtifactsRepo(); err != nil {
		return nil, err
	}

	commitHash, err := job.cloneAndCheckoutSource()
	if err != nil {
		return nil, err
	}
	result := &buildResult{CommitHash: commitHash}

	if err := job.generateEnvFile(); err != nil {
		return result, err
	}

	if err := job.generateBuildScript(); err != nil {
		return result, err
	}

	if _, err := os.Stat(job.buildScriptPath); os.IsNotExist(err) {
		job.logger.Info("Build script was not generated, skipping build. (This may be expected for some components)")
		return result, nil
	}

	if err := job.executeBuild(); err != nil {
		return result, err
	}
	if !job.spec.Artifacts.Push {
		job.logger.Info("spec.artifacts.push is false, skipping publish phase.")
		return result, nil
	}

	pushedYAML, err := job.executePublish()
	if err != nil {
		return result, err
	}

	result.PushedArtifactsYaml = pushedYAML
	return result, nil
}

type nativeBuildJob struct {
	reconciler *MacBuildReconciler
	ctx        context.Context
	logger     logr.Logger
	macBuild   buildv1alpha1.MacBuild
	spec       buildv1alpha1.MacBuildSpec // shortcut

	// Paths
	workspaceDir     string
	sourceDir        string
	artifactsRepoDir string
	buildScriptPath  string
	envFilePath      string
	pushedResultPath string
}

func newNativeBuildJob(r *MacBuildReconciler, ctx context.Context, macBuild buildv1alpha1.MacBuild) *nativeBuildJob {
	logger := logf.FromContext(ctx)

	// Create in 'os.TempDir()' (e.g., /var/folders/...)
	// 'workspaceDir' will be created in 'setupWorkspace'
	baseDir := os.TempDir()
	workspaceName := fmt.Sprintf("macbuild-%s-%d", macBuild.Name, time.Now().UnixNano())
	workspaceDir := filepath.Join(baseDir, workspaceName)

	return &nativeBuildJob{
		reconciler: r,
		ctx:        ctx,
		logger:     logger.WithValues("job", macBuild.Namespace, "workspace", workspaceDir),
		macBuild:   macBuild,
		spec:       macBuild.Spec,

		workspaceDir:     workspaceDir,
		sourceDir:        filepath.Join(workspaceDir, "source"),
		artifactsRepoDir: filepath.Join(workspaceDir, "artifacts"),
		buildScriptPath:  filepath.Join(workspaceDir, "build-package-artifacts.sh"),
		envFilePath:      filepath.Join(workspaceDir, "remote.env"),
		pushedResultPath: filepath.Join(workspaceDir, "pushed.yaml"),
	}
}

func (j *nativeBuildJob) setupWorkspace() error {
	j.logger.Info("Creating workspace", "dir", j.workspaceDir)
	if err := os.MkdirAll(j.workspaceDir, 0755); err != nil {
		return fmt.Errorf("failed to create temp workspace: %w", err)
	}
	return nil
}

func (j *nativeBuildJob) cleanup() {
	j.logger.Info("Cleaning up workspace", "dir", j.workspaceDir)
	if err := os.RemoveAll(j.workspaceDir); err != nil {
		j.logger.Error(err, "Failed to clean up workspace")
	}
}

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

func (j *nativeBuildJob) cloneArtifactsRepo() error {
	j.logger.Info("Cloning artifacts repository...")
	artifactsRepoURL := "https://github.com/PingCAP-QE/artifacts.git"
	cmd := exec.Command("git", "clone", "--depth=1", "--branch=main", artifactsRepoURL, j.artifactsRepoDir)
	if err := j.exec(cmd); err != nil {
		return fmt.Errorf("failed to clone artifacts repo: %w", err)
	}
	return nil
}

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

func (j *nativeBuildJob) createRunnableScript(wrapperName string, scriptContent string) (string, error) {
	scriptPath := filepath.Join(j.workspaceDir, wrapperName)
	fullContent := fmt.Sprintf("#!/bin/bash\nset -eo pipefail\n%s\n", scriptContent)

	if err := os.WriteFile(scriptPath, []byte(fullContent), 0755); err != nil {
		return "", fmt.Errorf("failed to create runnable script %s: %w", wrapperName, err)
	}
	return scriptPath, nil
}

func (j *nativeBuildJob) executeBuild() error {
	j.logger.Info("Executing build script (Build phase)...")

	scriptContent := fmt.Sprintf(`
source %s
bash %s
`, j.envFilePath, j.buildScriptPath)

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

func (j *nativeBuildJob) executePublish() (string, error) {
	j.logger.Info("Executing build script (Publish phase)...")
	releaseDir := filepath.Join(j.sourceDir, j.spec.Build.Component, "build")

	scriptContent := fmt.Sprintf(`
source %s
bash %s -p -w "%s" -o "%s"
`, j.envFilePath, j.buildScriptPath, releaseDir, j.pushedResultPath)

	publishScriptPath, err := j.createRunnableScript("run_publish.sh", scriptContent)
	if err != nil {
		return "", err
	}

	cmdPublish := exec.Command(publishScriptPath)
	buildDir := filepath.Join(j.sourceDir, j.spec.Build.Component)

	err = j.exec(cmdPublish, buildDir)
	if err != nil {
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
