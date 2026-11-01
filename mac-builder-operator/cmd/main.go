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

package main

import (
	"context"
	"crypto/tls"
	"flag"
	"fmt"
	"os"
	goruntime "runtime"
	"strings"
	"time"

	// Import all Kubernetes client auth plugins (e.g. Azure, GCP, OIDC, etc.)
	// to ensure that exec-entrypoint and run can make use of them.
	_ "k8s.io/client-go/plugin/pkg/client/auth"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/metrics/filters"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/identity"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/inventory"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/agent/state"
	"github.com/PingCAP-QE/ee-apps/mac-builder-operator/internal/controller"
	tektonv1beta1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	// +kubebuilder:scaffold:imports
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
	version  = "dev"
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))

	utilruntime.Must(buildv1alpha1.AddToScheme(scheme))

	// Register Tekton types so the manager can watch CustomRun objects.
	utilruntime.Must(tektonv1beta1.AddToScheme(scheme))
	// +kubebuilder:scaffold:scheme
}

// nolint:gocyclo
func main() {
	var metricsAddr string
	var metricsCertPath, metricsCertName, metricsCertKey string
	var webhookCertPath, webhookCertName, webhookCertKey string
	var enableLeaderElection bool
	var probeAddr string
	var secureMetrics bool
	var enableHTTP2 bool
	var tlsOpts []func(*tls.Config)
	var enableAgent, enableGC, enableCustomRun, enableMacAgent, enableScheduler bool
	var (
		buildTimeout, buildPollInterval, customRunPollInterval time.Duration
		offlineThreshold, macAgentRequeueInterval              time.Duration
		assignmentTimeout                                      time.Duration
	)
	var maxAssignmentAttempts int
	var artifactsRepoURL, artifactsRepoRevision, artifactsRepoCommit string
	var macBuildNamespace string
	var workerName, legacyWorkerName, workerArch, labelsFlag, agentVersion, stateFile string
	var capacity int
	var heartbeatInterval, staticRefreshInterval time.Duration

	// Controller-specific flags
	flag.BoolVar(&enableAgent, "enable-agent", false, "Enable the MacBuild agent reconciler. Runs on the Mac worker.")
	flag.BoolVar(&enableGC, "enable-gc", true, "Enable the MacBuild GC reconciler. Runs in the cluster.")
	flag.BoolVar(&enableMacAgent, "enable-macagent", false,
		"Enable the MacAgent controller (heartbeat readiness, schedulability and summary). Runs in the cluster.")
	flag.DurationVar(&offlineThreshold, "offline-threshold", 60*time.Second,
		"How long without a heartbeat before a MacAgent is marked Offline.")
	flag.DurationVar(&macAgentRequeueInterval, "macagent-requeue-interval", 30*time.Second,
		"How often the MacAgent controller re-evaluates heartbeat readiness.")
	flag.BoolVar(&enableScheduler, "enable-scheduler", false,
		"Enable the MacBuild scheduler (assigns Pending builds to MacAgents). Runs in the cluster.")
	flag.DurationVar(&assignmentTimeout, "assignment-timeout", 2*time.Minute,
		"How long an Assigned build may wait for its agent to start before being reassigned.")
	flag.IntVar(&maxAssignmentAttempts, "max-assignment-attempts", 3,
		"Maximum assignment attempts before a MacBuild is marked AssignmentFailed.")
	flag.BoolVar(&enableCustomRun, "enable-customrun", true,
		"Enable the MacBuild CustomRun reconciler (Tekton custom task backed by MacBuild). Runs in the cluster.")
	flag.StringVar(&macBuildNamespace, "macbuild-namespace", "default",
		"Namespace where the CustomRun reconciler creates MacBuild objects.")
	flag.DurationVar(&customRunPollInterval, "customrun-poll-interval", 30*time.Second,
		"How often running CustomRuns are re-checked for MacBuild status changes.")
	flag.DurationVar(&buildTimeout, "build-timeout", 24*time.Hour,
		"Maximum time a MacBuild may stay in Building before the agent marks it failed.")
	flag.DurationVar(&buildPollInterval, "build-poll-interval", 5*time.Minute,
		"How often the agent rechecks Building jobs assigned to other workers.")
	flag.StringVar(&artifactsRepoURL, "artifacts-repo-url", controller.DefaultArtifactsScriptRepoURL,
		"Git repository URL used to generate build scripts for artifact packaging.")
	flag.StringVar(&artifactsRepoRevision, "artifacts-repo-revision", controller.DefaultArtifactsScriptRepoRevision,
		"Immutable tag or commit to check out from the artifacts repo before generating build scripts.")
	flag.StringVar(&artifactsRepoCommit, "artifacts-repo-commit", controller.DefaultArtifactsScriptExpectedCommit,
		"Expected full commit SHA for the pinned artifacts repo revision.")
	flag.StringVar(&workerName, "agent-name", "",
		"MacAgent name override; default is derived from the machine's IOPlatformUUID.")
	flag.StringVar(&legacyWorkerName, "worker-name", "",
		"Deprecated: use --agent-name.")
	flag.StringVar(&workerArch, "worker-arch", goruntime.GOARCH,
		"CPU architecture this worker can handle. Supported values: amd64, arm64.")
	flag.StringVar(&labelsFlag, "labels", "",
		"Comma-separated key=value labels stored on the MacAgent (e.g. tibuild.pingcap.net/pool=mac-mini).")
	flag.IntVar(&capacity, "capacity", 1, "Maximum concurrent builds this worker accepts.")
	flag.DurationVar(&heartbeatInterval, "heartbeat-interval", 20*time.Second,
		"How often the agent reports its MacAgent heartbeat.")
	flag.DurationVar(&staticRefreshInterval, "static-refresh-interval", 10*time.Minute,
		"How often the agent refreshes OS/hardware inventory.")
	flag.StringVar(&agentVersion, "agent-version", version,
		"Agent version reported in MacAgent status.")
	flag.StringVar(&stateFile, "state-file", "",
		"Path to the in-flight build state file (default: the per-user config dir).")

	// General flags
	flag.StringVar(&metricsAddr, "metrics-bind-address", "0", "The address the metrics endpoint binds to. "+
		"Use :8443 for HTTPS or :8080 for HTTP, or leave as 0 to disable the metrics service.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager. "+
			"Enabling this will ensure there is only one active controller manager.")
	flag.BoolVar(&secureMetrics, "metrics-secure", true,
		"If set, the metrics endpoint is served securely via HTTPS. Use --metrics-secure=false to use HTTP instead.")
	flag.StringVar(&webhookCertPath, "webhook-cert-path", "", "The directory that contains the webhook certificate.")
	flag.StringVar(&webhookCertName, "webhook-cert-name", "tls.crt", "The name of the webhook certificate file.")
	flag.StringVar(&webhookCertKey, "webhook-cert-key", "tls.key", "The name of the webhook key file.")
	flag.StringVar(&metricsCertPath, "metrics-cert-path", "",
		"The directory that contains the metrics server certificate.")
	flag.StringVar(&metricsCertName, "metrics-cert-name", "tls.crt", "The name of the metrics server certificate file.")
	flag.StringVar(&metricsCertKey, "metrics-cert-key", "tls.key", "The name of the metrics server key file.")
	flag.BoolVar(&enableHTTP2, "enable-http2", false,
		"If set, HTTP/2 will be enabled for the metrics and webhook servers")
	opts := zap.Options{
		Development: true,
	}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	if legacyWorkerName != "" {
		setupLog.Info("--worker-name is deprecated; use --agent-name", "workerName", legacyWorkerName)
		if workerName == "" {
			workerName = legacyWorkerName
		}
	}
	labels, err := parseLabels(labelsFlag)
	if err != nil {
		setupLog.Error(err, "invalid --labels")
		os.Exit(1)
	}

	if !enableAgent && !enableGC && !enableCustomRun && !enableMacAgent && !enableScheduler {
		setupLog.Info("No controllers enabled. " +
			"Use --enable-agent, --enable-gc, --enable-macagent, --enable-scheduler " +
			"or --enable-customrun to start a controller.")
		os.Exit(1)
	}
	if enableAgent && buildTimeout <= 0 {
		setupLog.Info("build-timeout must be greater than zero")
		os.Exit(1)
	}
	if enableAgent && buildPollInterval <= 0 {
		setupLog.Info("build-poll-interval must be greater than zero")
		os.Exit(1)
	}
	if enableAgent && heartbeatInterval <= 0 {
		setupLog.Info("heartbeat-interval must be greater than zero")
		os.Exit(1)
	}
	if enableAgent && staticRefreshInterval <= 0 {
		setupLog.Info("static-refresh-interval must be greater than zero")
		os.Exit(1)
	}
	if enableAgent &&
		(int32(capacity) < buildv1alpha1.MinMacAgentCapacity ||
			int32(capacity) > buildv1alpha1.MaxMacAgentCapacity) {
		setupLog.Info("capacity must be between 1 and 8", "capacity", capacity)
		os.Exit(1)
	}
	if enableMacAgent && offlineThreshold <= 0 {
		setupLog.Info("offline-threshold must be greater than zero")
		os.Exit(1)
	}
	if enableMacAgent && macAgentRequeueInterval <= 0 {
		setupLog.Info("macagent-requeue-interval must be greater than zero")
		os.Exit(1)
	}
	if enableScheduler && assignmentTimeout <= 0 {
		setupLog.Info("assignment-timeout must be greater than zero")
		os.Exit(1)
	}
	if enableScheduler && maxAssignmentAttempts < 1 {
		setupLog.Info("max-assignment-attempts must be at least 1")
		os.Exit(1)
	}
	if enableAgent {
		normalizedWorkerName, normalizedWorkerArch, err := validateAgentIdentity(workerName, workerArch)
		if err != nil {
			setupLog.Info(err.Error(), "workerName", workerName, "workerArch", workerArch)
			os.Exit(1)
		}
		workerName = normalizedWorkerName
		workerArch = normalizedWorkerArch
	}

	artifactsScriptSource, err := (controller.ArtifactsScriptSourceConfig{
		URL:            artifactsRepoURL,
		Revision:       artifactsRepoRevision,
		ExpectedCommit: artifactsRepoCommit,
	}).Normalize()
	if enableAgent && err != nil {
		setupLog.Error(err, "invalid artifacts repo source configuration")
		os.Exit(1)
	}

	// if the enable-http2 flag is false (the default), http/2 should be disabled
	// due to its vulnerabilities. More specifically, disabling http/2 will
	// prevent from being vulnerable to the HTTP/2 Stream Cancellation and
	// Rapid Reset CVEs. For more information see:
	// - https://github.com/advisories/GHSA-qppj-fm5r-hxr3
	// - https://github.com/advisories/GHSA-4374-p667-p6c8
	disableHTTP2 := func(c *tls.Config) {
		setupLog.Info("disabling http/2")
		c.NextProtos = []string{"http/1.1"}
	}

	if !enableHTTP2 {
		tlsOpts = append(tlsOpts, disableHTTP2)
	}

	// Initial webhook TLS options
	webhookTLSOpts := tlsOpts
	webhookServerOptions := webhook.Options{
		TLSOpts: webhookTLSOpts,
	}

	if len(webhookCertPath) > 0 {
		setupLog.Info("Initializing webhook certificate watcher using provided certificates",
			"webhook-cert-path", webhookCertPath, "webhook-cert-name", webhookCertName, "webhook-cert-key", webhookCertKey)

		webhookServerOptions.CertDir = webhookCertPath
		webhookServerOptions.CertName = webhookCertName
		webhookServerOptions.KeyName = webhookCertKey
	}

	webhookServer := webhook.NewServer(webhookServerOptions)

	// Metrics endpoint is enabled in 'config/default/kustomization.yaml'. The Metrics options configure the server.
	// More info:
	// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.22.1/pkg/metrics/server
	// - https://book.kubebuilder.io/reference/metrics.html
	metricsServerOptions := metricsserver.Options{
		BindAddress:   metricsAddr,
		SecureServing: secureMetrics,
		TLSOpts:       tlsOpts,
	}

	if secureMetrics {
		// FilterProvider is used to protect the metrics endpoint with authn/authz.
		// These configurations ensure that only authorized users and service accounts
		// can access the metrics endpoint. The RBAC are configured in 'config/rbac/kustomization.yaml'. More info:
		// https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.22.1/pkg/metrics/filters#WithAuthenticationAndAuthorization
		metricsServerOptions.FilterProvider = filters.WithAuthenticationAndAuthorization
	}

	// If the certificate is not specified, controller-runtime will automatically
	// generate self-signed certificates for the metrics server. While convenient for development and testing,
	// this setup is not recommended for production.
	//
	// TODO(user): If you enable certManager, uncomment the following lines:
	// - [METRICS-WITH-CERTS] at config/default/kustomization.yaml to generate and use certificates
	// managed by cert-manager for the metrics server.
	// - [PROMETHEUS-WITH-CERTS] at config/prometheus/kustomization.yaml for TLS certification.
	if len(metricsCertPath) > 0 {
		setupLog.Info("Initializing metrics certificate watcher using provided certificates",
			"metrics-cert-path", metricsCertPath, "metrics-cert-name", metricsCertName, "metrics-cert-key", metricsCertKey)

		metricsServerOptions.CertDir = metricsCertPath
		metricsServerOptions.CertName = metricsCertName
		metricsServerOptions.KeyName = metricsCertKey
	}

	cacheOptions := cache.Options{}
	if enableAgent && macBuildNamespace != "" {
		// The worker only watches MacBuilds in its build namespace. This keeps
		// the informer cache and the worker RBAC namespace-scoped.
		cacheOptions.ByObject = map[client.Object]cache.ByObject{
			&buildv1alpha1.MacBuild{}: {
				Namespaces: map[string]cache.Config{macBuildNamespace: {}},
			},
		}
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsServerOptions,
		WebhookServer:          webhookServer,
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "1e1ceb52.tibuild.pingcap.net",
		Cache:                  cacheOptions,
		// LeaderElectionReleaseOnCancel defines if the leader should step down voluntarily
		// when the Manager ends. This requires the binary to immediately end when the
		// Manager is stopped, otherwise, this setting is unsafe. Setting this significantly
		// speeds up voluntary leader transitions as the new leader don't have to wait
		// LeaseDuration time first.
		//
		// In the default scaffold provided, the program ends immediately after
		// the manager stops, so would be fine to enable this option. However,
		// if you are doing or is intended to do any operation such as perform cleanups
		// after the manager stops then its usage might be unsafe.
		// LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	if enableAgent {
		machine, err := (identity.Resolver{
			Runner:   identity.ExecRunner{},
			Override: workerName,
			Hostname: os.Hostname,
		}).Resolve(context.Background())
		if err != nil {
			setupLog.Error(err, "unable to determine stable agent identity")
			os.Exit(1)
		}
		workerName = machine.Name
		setupLog.Info("Starting manager with worker identity",
			"workerID", workerName, "workerArch", workerArch,
			"machineIDSource", machine.Source, "machineID", machine.Value)

		statePath := stateFile
		if statePath == "" {
			statePath, err = state.DefaultPath()
			if err != nil {
				setupLog.Error(err, "unable to resolve the agent state path")
				os.Exit(1)
			}
		}
		stateStore := state.NewStore(statePath)

		if err := (&controller.MacBuildReconciler{
			Client:                mgr.GetClient(),
			APIReader:             mgr.GetAPIReader(),
			Scheme:                mgr.GetScheme(),
			WorkerID:              workerName,
			WorkerArch:            workerArch,
			BuildTimeout:          buildTimeout,
			BuildPollInterval:     buildPollInterval,
			ArtifactsScriptSource: artifactsScriptSource,
			StateStore:            stateStore,
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to create controller", "controller", "MacBuildAgent")
			os.Exit(1)
		}
		if err := mgr.Add(&controller.MacAgentHeartbeat{
			Client:                mgr.GetClient(),
			APIReader:             mgr.GetAPIReader(),
			AgentName:             workerName,
			AgentArch:             workerArch,
			AgentVersion:          agentVersion,
			Capacity:              int32(capacity),
			Labels:                labels,
			Machine:               machine,
			Inventory:             inventory.NewProvider(),
			HeartbeatInterval:     heartbeatInterval,
			StaticRefreshInterval: staticRefreshInterval,
		}); err != nil {
			setupLog.Error(err, "unable to add MacAgent heartbeat")
			os.Exit(1)
		}
		setupLog.Info("MacBuild Agent controller enabled.", "macAgent", workerName)
	}

	if enableGC {
		if err := (&controller.MacBuildGCReconciler{
			Client: mgr.GetClient(),
			Scheme: mgr.GetScheme(),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to create controller", "controller", "MacBuildGC")
			os.Exit(1)
		}
		setupLog.Info("MacBuild GC controller enabled.")
	}

	if enableMacAgent {
		if err := (&controller.MacAgentReconciler{
			Client:            mgr.GetClient(),
			Scheme:            mgr.GetScheme(),
			MacBuildNamespace: macBuildNamespace,
			OfflineThreshold:  offlineThreshold,
			RequeueInterval:   macAgentRequeueInterval,
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to create controller", "controller", "MacAgent")
			os.Exit(1)
		}
		setupLog.Info("MacAgent controller enabled.", "macBuildNamespace", macBuildNamespace)
	}

	if enableScheduler {
		if err := (&controller.MacBuildScheduler{
			Client:                mgr.GetClient(),
			APIReader:             mgr.GetAPIReader(),
			Scheme:                mgr.GetScheme(),
			MacBuildNamespace:     macBuildNamespace,
			AssignmentTimeout:     assignmentTimeout,
			MaxAssignmentAttempts: int32(maxAssignmentAttempts),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to create controller", "controller", "MacBuildScheduler")
			os.Exit(1)
		}
		setupLog.Info("MacBuild scheduler enabled.", "macBuildNamespace", macBuildNamespace)
	}

	if enableCustomRun {
		if err := (&controller.MacBuildCustomRunReconciler{
			Client:            mgr.GetClient(),
			Scheme:            mgr.GetScheme(),
			MacBuildNamespace: macBuildNamespace,
			PollInterval:      customRunPollInterval,
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to create controller", "controller", "MacBuildCustomRun")
			os.Exit(1)
		}
		setupLog.Info("MacBuild CustomRun controller enabled.", "macBuildNamespace", macBuildNamespace)
	}

	// +kubebuilder:scaffold:builder

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}

func validateAgentIdentity(agentName string, agentArch string) (string, string, error) {
	normalizedAgentName := strings.TrimSpace(agentName)
	normalizedAgentArch := buildv1alpha1.NormalizeBuildArch(agentArch)
	if !buildv1alpha1.IsSupportedBuildArch(normalizedAgentArch) {
		return "", "", fmt.Errorf("worker-arch must be one of amd64 or arm64")
	}

	return normalizedAgentName, normalizedAgentArch, nil
}

func parseLabels(value string) (map[string]string, error) {
	labels := make(map[string]string)
	for _, item := range strings.Split(value, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		key, labelValue, ok := strings.Cut(item, "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" {
			return nil, fmt.Errorf("invalid label %q: expected key=value", item)
		}
		labels[key] = strings.TrimSpace(labelValue)
	}
	return labels, nil
}
