package controller

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const (
	testCredentialsNamespace  = "ee-cd"
	testCredentialsSecretName = "github-git-credentials"
	testCredentialsToken      = "injected-secret-token-for-tests"
	testCredentialsUsername   = "x-access-token"
)

func newGitCredentialsJob(
	t *testing.T,
	source buildv1alpha1.SourceSpec,
	objects ...client.Object,
) *nativeBuildJob {
	t.Helper()

	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatalf("add corev1 scheme: %v", err)
	}

	macBuild := newTestMacBuild()
	macBuild.Namespace = testCredentialsNamespace
	macBuild.Spec.Source = source

	job := newNativeBuildJob(context.Background(), macBuild, ArtifactsScriptSourceConfig{})
	job.credentialsReader = fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()

	if err := job.setupWorkspace(); err != nil {
		t.Fatalf("setup workspace: %v", err)
	}
	t.Cleanup(job.cleanup)

	return job
}

func basicAuthSecret(name, password string) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: testCredentialsNamespace, Name: name},
		Type:       corev1.SecretTypeBasicAuth,
		Data: map[string][]byte{
			gitUsernameKey: []byte(testCredentialsUsername),
			gitPasswordKey: []byte(password),
		},
	}
}

func TestResolveSourceGitCredentialsBasicAuth(t *testing.T) {
	t.Parallel()

	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "https://github.com/pingcap-inc/tici.git",
		GitRef:        "main",
		GitSecretRef:  stringPtr(testCredentialsSecretName),
	}, basicAuthSecret(testCredentialsSecretName, testCredentialsToken))

	creds, err := job.resolveSourceGitCredentials(job.ctx)
	if err != nil {
		t.Fatalf("resolve credentials: %v", err)
	}
	if creds == nil {
		t.Fatal("expected materialized credentials")
	}

	if creds.kind != gitCredentialKindBasicAuth {
		t.Fatalf("expected basic-auth kind, got %q", creds.kind)
	}

	credentialsPath := filepath.Join(job.workspaceDir, gitCredentialsDirName, gitCredentialsFileName)
	content, err := os.ReadFile(credentialsPath)
	if err != nil {
		t.Fatalf("read .git-credentials: %v", err)
	}
	want := "https://x-access-token:" + testCredentialsToken + "@github.com\n"
	if string(content) != want {
		t.Fatalf("unexpected .git-credentials content: %q", string(content))
	}

	info, err := os.Stat(credentialsPath)
	if err != nil {
		t.Fatalf("stat .git-credentials: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("expected .git-credentials mode 0600, got %o", perm)
	}

	args := strings.Join(creds.gitArgs(), " ")
	if !strings.Contains(args, "credential.helper=") || !strings.Contains(args, "store --file="+credentialsPath) {
		t.Fatalf("expected credential helper overrides, got %q", args)
	}

	// The token must never appear in the arguments or environment that are
	// logged with the git command; only on-disk (0600) files carry it.
	env := strings.Join(creds.gitEnv(), " ")
	if strings.Contains(args, testCredentialsToken) || strings.Contains(env, testCredentialsToken) {
		t.Fatal("token leaked into git arguments or environment")
	}

	if desc := creds.describe(); !strings.Contains(desc, testCredentialsSecretName) || strings.Contains(desc, testCredentialsToken) {
		t.Fatalf("expected a log-safe description naming the secret, got %q", desc)
	}

	creds.cleanup()
	if _, err := os.Stat(filepath.Join(job.workspaceDir, gitCredentialsDirName)); !os.IsNotExist(err) {
		t.Fatalf("expected credentials dir to be removed, stat err: %v", err)
	}
}

func TestResolveSourceGitCredentialsSSH(t *testing.T) {
	t.Parallel()

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: testCredentialsNamespace, Name: testCredentialsSecretName},
		Type:       corev1.SecretTypeSSHAuth,
		Data: map[string][]byte{
			gitSSHPrivateKeyKey: []byte("test-ssh-private-key-placeholder\n"),
			gitSSHKnownHostsKey: []byte("github.com ssh-ed25519 AAAA\n"),
			"id_ed25519.pub":    []byte("ssh-ed25519 AAAA user@host\n"),
		},
	}
	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "git@github.com:pingcap-inc/tici.git",
		GitRef:        "main",
		GitSecretRef:  stringPtr(testCredentialsSecretName),
	}, secret)

	creds, err := job.resolveSourceGitCredentials(job.ctx)
	if err != nil {
		t.Fatalf("resolve credentials: %v", err)
	}
	if creds == nil || creds.kind != gitCredentialKindSSH {
		t.Fatalf("expected ssh credentials, got %#v", creds)
	}

	sshDir := filepath.Join(job.workspaceDir, gitCredentialsDirName, gitSSHDirName)
	for name, wantMode := range map[string]os.FileMode{
		gitSSHPrivateKeyKey: 0o600,
		gitSSHKnownHostsKey: 0o644,
		"id_ed25519.pub":    0o600,
	} {
		info, err := os.Stat(filepath.Join(sshDir, name))
		if err != nil {
			t.Fatalf("stat %s: %v", name, err)
		}
		if perm := info.Mode().Perm(); perm != wantMode {
			t.Fatalf("expected %s mode %o, got %o", name, wantMode, perm)
		}
	}

	env := strings.Join(creds.gitEnv(), " ")
	if !strings.Contains(env, "GIT_SSH_COMMAND=ssh -i "+filepath.Join(sshDir, gitSSHPrivateKeyKey)) {
		t.Fatalf("expected GIT_SSH_COMMAND with -i, got %q", env)
	}
	if !strings.Contains(env, "-o UserKnownHostsFile="+filepath.Join(sshDir, gitSSHKnownHostsKey)) {
		t.Fatalf("expected UserKnownHostsFile override, got %q", env)
	}
	if strings.Contains(env, "test-ssh-private-key-placeholder") {
		t.Fatal("private key leaked into git environment")
	}
}

func TestResolveSourceGitCredentialsUsesReferencedSecret(t *testing.T) {
	t.Parallel()

	referenced := basicAuthSecret(testCredentialsSecretName, testCredentialsToken)
	unreferenced := basicAuthSecret("other-git-credentials", "other-token")

	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "https://github.com/pingcap-inc/tici.git",
		GitRef:        "main",
		GitSecretRef:  stringPtr(referenced.Name),
	}, referenced, unreferenced)

	creds, err := job.resolveSourceGitCredentials(job.ctx)
	if err != nil {
		t.Fatalf("resolve credentials: %v", err)
	}
	if creds == nil || creds.secretName != referenced.Name {
		t.Fatalf("expected the referenced secret to be used, got %#v", creds)
	}
}

func TestResolveSourceGitCredentialsDisabled(t *testing.T) {
	t.Parallel()

	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "https://github.com/pingcap-inc/tici.git",
		GitRef:        "main",
	})

	creds, err := job.resolveSourceGitCredentials(job.ctx)
	if err != nil {
		t.Fatalf("resolve credentials: %v", err)
	}
	if creds != nil {
		t.Fatalf("expected no credentials, got %#v", creds)
	}
	if creds.gitArgs() != nil || creds.gitEnv() != nil {
		t.Fatal("nil credentials must produce no git overrides")
	}
	creds.cleanup() // must be a no-op on nil
}

func TestResolveSourceGitCredentialsMissingSecret(t *testing.T) {
	t.Parallel()

	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "https://github.com/pingcap-inc/tici.git",
		GitRef:        "main",
		GitSecretRef:  stringPtr("does-not-exist"),
	})

	_, err := job.resolveSourceGitCredentials(job.ctx)
	if err == nil {
		t.Fatal("expected an error for a missing secret")
	}
	if !strings.Contains(err.Error(), "does-not-exist") {
		t.Fatalf("expected error to name the secret, got %v", err)
	}
}

func TestResolveSourceGitCredentialsUnsupportedSecret(t *testing.T) {
	t.Parallel()

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: testCredentialsNamespace, Name: testCredentialsSecretName},
		Data:       map[string][]byte{"some-other-key": []byte("value")},
	}
	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "https://github.com/pingcap-inc/tici.git",
		GitRef:        "main",
		GitSecretRef:  stringPtr(testCredentialsSecretName),
	}, secret)

	if _, err := job.resolveSourceGitCredentials(job.ctx); err == nil {
		t.Fatal("expected an error for an unsupported secret shape")
	}
}

func TestResolveSourceGitCredentialsOpaqueSecretDetectedByKeys(t *testing.T) {
	t.Parallel()

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: testCredentialsNamespace, Name: testCredentialsSecretName},
		// Opaque secret: no kubernetes.io type. The keys drive the detection.
		Data: map[string][]byte{
			gitUsernameKey: []byte("x-access-token"),
			gitTokenKey:    []byte(testCredentialsToken),
		},
	}
	job := newGitCredentialsJob(t, buildv1alpha1.SourceSpec{
		GitRepository: "https://github.com/pingcap-inc/tici.git",
		GitRef:        "main",
		GitSecretRef:  stringPtr(testCredentialsSecretName),
	}, secret)

	creds, err := job.resolveSourceGitCredentials(job.ctx)
	if err != nil {
		t.Fatalf("resolve credentials: %v", err)
	}
	if creds == nil || creds.kind != gitCredentialKindBasicAuth {
		t.Fatalf("expected opaque secret with token key to resolve as basic-auth, got %#v", creds)
	}
}

func TestMaterializeGitCredentialsRejectsMissingBasicAuthKeys(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	secret := &corev1.Secret{
		Type: corev1.SecretTypeBasicAuth,
		Data: map[string][]byte{gitUsernameKey: []byte("x-access-token")},
	}
	if _, err := materializeGitCredentials(secret, "https://github.com/pingcap-inc/tici.git", dir); err == nil {
		t.Fatal("expected an error for a basic-auth secret without a password/token")
	}
}

func TestMaterializeGitCredentialsRejectsBasicAuthForNonHTTPURL(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	secret := &corev1.Secret{
		Type: corev1.SecretTypeBasicAuth,
		Data: map[string][]byte{
			gitUsernameKey: []byte("x-access-token"),
			gitPasswordKey: []byte(testCredentialsToken),
		},
	}
	if _, err := materializeGitCredentials(secret, "/tmp/local-repo", dir); err == nil {
		t.Fatal("expected an error when the host cannot be derived from the repository URL")
	}
}

func TestMaterializeSSHRejectsUnsafeFileName(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	secret := &corev1.Secret{
		Type: corev1.SecretTypeSSHAuth,
		Data: map[string][]byte{
			gitSSHPrivateKeyKey: []byte("key"),
			"../escape":         []byte("evil"),
		},
	}
	if _, err := materializeGitCredentials(secret, "git@github.com:org/repo.git", dir); err == nil {
		t.Fatal("expected an error for a secret key escaping the ssh dir")
	}
}

func TestMaterializePackedGitCredentials(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	secret := &corev1.Secret{
		Data: map[string][]byte{
			gitCredentialsFileName: []byte("https://x-access-token:packed-token@github.com\n"),
		},
	}
	creds, err := materializeGitCredentials(secret, "https://github.com/pingcap-inc/tici.git", dir)
	if err != nil {
		t.Fatalf("materialize packed credentials: %v", err)
	}
	if creds.kind != gitCredentialKindBasicAuth {
		t.Fatalf("expected basic-auth kind, got %q", creds.kind)
	}

	content, err := os.ReadFile(filepath.Join(dir, gitCredentialsFileName))
	if err != nil {
		t.Fatalf("read packed .git-credentials: %v", err)
	}
	if string(content) != "https://x-access-token:packed-token@github.com\n" {
		t.Fatalf("packed credentials must be written verbatim, got %q", string(content))
	}
}

func TestGitRepositoryHost(t *testing.T) {
	t.Parallel()

	cases := map[string]string{
		"https://github.com/pingcap-inc/tici.git": "github.com",
		"https://github.com/org/repo":             "github.com",
		"https://ghe.example.com:8443/org/repo":   "ghe.example.com",
		"ssh://git@github.com/org/repo.git":       "github.com",
		"git@github.com:pingcap-inc/tici.git":     "github.com",
		"/tmp/local-repo":                         "",
		"":                                        "",
	}

	for repositoryURL, want := range cases {
		if got := gitRepositoryHost(repositoryURL); got != want {
			t.Fatalf("gitRepositoryHost(%q) = %q, want %q", repositoryURL, got, want)
		}
	}
}
