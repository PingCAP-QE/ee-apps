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
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// gitCredentialKind identifies how the materialized git credentials authenticate.
type gitCredentialKind string

const (
	gitCredentialKindBasicAuth gitCredentialKind = "basic-auth"
	gitCredentialKindSSH       gitCredentialKind = "ssh"
)

const (
	// gitCredentialsDirName is the workspace subdirectory holding the per-build
	// materialized git credentials. The whole directory is removed once the
	// source checkout (including submodules) is done.
	gitCredentialsDirName = "git-credentials"

	gitCredentialsFileName = ".git-credentials"
	gitSSHDirName          = ".ssh"
	gitSSHPrivateKeyKey    = "ssh-privatekey"
	gitSSHKnownHostsKey    = "known_hosts"
	gitUsernameKey         = "username"
	gitPasswordKey         = "password"
	gitTokenKey            = "token"
)

// gitCredentialPrivateKeyKeys are the accepted SSH private key data keys, in
// precedence order. `ssh-privatekey` is the kubernetes.io/ssh-auth convention;
// the `id_*` names match the Tekton ssh-directory workspace convention.
var gitCredentialPrivateKeyKeys = []string{gitSSHPrivateKeyKey, "id_ed25519", "id_rsa", "id_ecdsa"}

// gitCredentials is the per-build git credential state: files materialized under
// dir plus the git arguments/environment that consume them. A nil value means an
// anonymous clone. Secrets are only ever stored on disk (0600/0700) and in the
// git child process environment — never in logs and never in the command line.
type gitCredentials struct {
	kind       gitCredentialKind
	dir        string
	secretName string
	args       []string
	env        []string
}

// gitArgs returns the leading `git -c ...` overrides for git commands.
func (c *gitCredentials) gitArgs() []string {
	if c == nil {
		return nil
	}
	return c.args
}

// gitEnv returns the extra environment variables for git commands.
func (c *gitCredentials) gitEnv() []string {
	if c == nil {
		return nil
	}
	return c.env
}

// cleanup removes the materialized credential files. It is safe on nil and
// idempotent (the workspace cleanup would remove the directory anyway).
func (c *gitCredentials) cleanup() {
	if c == nil || c.dir == "" {
		return
	}
	_ = os.RemoveAll(c.dir)
}

// describe returns a log-safe description (kind and source secret, no values).
func (c *gitCredentials) describe() string {
	if c == nil {
		return "anonymous"
	}
	return fmt.Sprintf("%s (secret %q)", c.kind, c.secretName)
}

// resolveSourceGitCredentials reads and materializes the git credentials Secret
// for the source clone: the MacBuild's source.gitSecretRef when set, otherwise
// the operator's namespace-level default Secret. It returns (nil, nil) when no
// secret is configured — public repositories keep cloning anonymously.
func (j *nativeBuildJob) resolveSourceGitCredentials(ctx context.Context) (*gitCredentials, error) {
	if j.credentialsReader == nil {
		return nil, nil
	}

	secretName := strings.TrimSpace(j.defaultGitCredentialsSecret)
	if ref := j.spec.Source.GitSecretRef; ref != nil && strings.TrimSpace(*ref) != "" {
		secretName = strings.TrimSpace(*ref)
	}
	if secretName == "" {
		return nil, nil
	}

	var secret corev1.Secret
	key := client.ObjectKey{Namespace: j.macBuild.Namespace, Name: secretName}
	if err := j.credentialsReader.Get(ctx, key, &secret); err != nil {
		return nil, fmt.Errorf("read git credentials secret %q in namespace %q: %w", secretName, j.macBuild.Namespace, err)
	}

	dir := filepath.Join(j.workspaceDir, gitCredentialsDirName)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("create git credentials dir: %w", err)
	}

	creds, err := materializeGitCredentials(&secret, j.spec.Source.GitRepository, dir)
	if err != nil {
		_ = os.RemoveAll(dir)
		return nil, fmt.Errorf("git credentials secret %q: %w", secretName, err)
	}
	creds.secretName = secretName
	return creds, nil
}

// materializeGitCredentials writes the secret content under dir and returns the
// git overrides that consume it. Supported secret shapes:
//   - kubernetes.io/basic-auth (`username` + `password`), a `token` key, or a
//     packed `.git-credentials` key → HTTPS credentials;
//   - kubernetes.io/ssh-auth (`ssh-privatekey`) or ssh key files (`id_ed25519`,
//     `id_rsa`, `known_hosts`, `config`, ...) → SSH credentials.
func materializeGitCredentials(secret *corev1.Secret, repositoryURL, dir string) (*gitCredentials, error) {
	if _, ok := secret.Data[gitCredentialsFileName]; ok {
		return materializeBasicAuth(secret, repositoryURL, dir, gitCredentialsFileName)
	}

	switch secret.Type {
	case corev1.SecretTypeBasicAuth:
		return materializeBasicAuth(secret, repositoryURL, dir, "")
	case corev1.SecretTypeSSHAuth:
		return materializeSSH(secret, dir)
	}

	if hasAnyDataKey(secret, gitCredentialPrivateKeyKeys...) {
		return materializeSSH(secret, dir)
	}
	if hasAnyDataKey(secret, gitUsernameKey, gitPasswordKey, gitTokenKey) {
		return materializeBasicAuth(secret, repositoryURL, dir, "")
	}

	return nil, fmt.Errorf(
		"unsupported secret data: expected kubernetes.io/basic-auth, kubernetes.io/ssh-auth, "+
			"a %q key, or ssh key files", gitCredentialsFileName)
}

// materializeBasicAuth materializes HTTPS credentials as `.git-credentials`.
// `packedKey` names an existing `.git-credentials` data key to write verbatim;
// when empty, a credentials line is synthesized from the username/password (or
// token) keys for the repository's host.
func materializeBasicAuth(secret *corev1.Secret, repositoryURL, dir, packedKey string) (*gitCredentials, error) {
	credentialsPath := filepath.Join(dir, gitCredentialsFileName)

	content := secret.Data[packedKey]
	if packedKey == "" {
		username := strings.TrimSpace(string(secret.Data[gitUsernameKey]))
		password := string(secret.Data[gitPasswordKey])
		if password == "" {
			password = string(secret.Data[gitTokenKey])
		}
		if username == "" || password == "" {
			return nil, fmt.Errorf(
				"basic-auth secret requires non-empty %q and %q (or %q) keys", gitUsernameKey, gitPasswordKey, gitTokenKey)
		}

		host := gitRepositoryHost(repositoryURL)
		if host == "" {
			return nil, fmt.Errorf(
				"cannot derive a host from repository URL %q for basic-auth credentials (use an HTTPS URL or SSH keys)", repositoryURL)
		}

		credentialsURL := (&url.URL{Scheme: "https", Host: host, User: url.UserPassword(username, password)}).String()
		content = []byte(credentialsURL + "\n")
	}

	if err := writeSecretFile(credentialsPath, content, 0o600); err != nil {
		return nil, err
	}

	return &gitCredentials{
		kind: gitCredentialKindBasicAuth,
		dir:  dir,
		args: []string{
			// Reset the helper list so the worker's global helpers (e.g. the
			// macOS keychain) cannot shadow or override the injected secret.
			"-c", "credential.helper=",
			"-c", "credential.helper=store --file=" + credentialsPath,
		},
		env: []string{"GIT_TERMINAL_PROMPT=0"},
	}, nil
}

// materializeSSH materializes the ssh key files under `.ssh` and points git at
// them via GIT_SSH_COMMAND, mirroring the Tekton pingcap-git-clone task's
// ssh-directory workspace handling.
func materializeSSH(secret *corev1.Secret, dir string) (*gitCredentials, error) {
	privateKeyName := ""
	for _, candidate := range gitCredentialPrivateKeyKeys {
		if _, ok := secret.Data[candidate]; ok {
			privateKeyName = candidate
			break
		}
	}
	if privateKeyName == "" {
		return nil, fmt.Errorf("ssh credentials require one of %q keys", strings.Join(gitCredentialPrivateKeyKeys, ", "))
	}

	sshDir := filepath.Join(dir, gitSSHDirName)
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		return nil, fmt.Errorf("create ssh credentials dir: %w", err)
	}

	names := make([]string, 0, len(secret.Data))
	for name := range secret.Data {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, name := range names {
		fileName, err := safeSSHFileName(name)
		if err != nil {
			return nil, err
		}
		mode := os.FileMode(0o600)
		if fileName == gitSSHKnownHostsKey {
			mode = 0o644
		}
		if err := writeSecretFile(filepath.Join(sshDir, fileName), secret.Data[name], mode); err != nil {
			return nil, err
		}
	}

	sshCommand := fmt.Sprintf(
		"ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
		filepath.Join(sshDir, privateKeyName),
	)
	if _, ok := secret.Data[gitSSHKnownHostsKey]; ok {
		sshCommand += fmt.Sprintf(" -o UserKnownHostsFile=%s", filepath.Join(sshDir, gitSSHKnownHostsKey))
	}

	return &gitCredentials{
		kind: gitCredentialKindSSH,
		dir:  dir,
		env:  []string{"GIT_TERMINAL_PROMPT=0", "GIT_SSH_COMMAND=" + sshCommand},
	}, nil
}

// safeSSHFileName validates a Secret data key used as a file name under `.ssh`
// so a Secret can never write outside the credentials directory.
func safeSSHFileName(name string) (string, error) {
	clean := strings.TrimSpace(name)
	if clean == "" || clean == "." || clean == ".." ||
		clean != filepath.Base(clean) || strings.ContainsAny(clean, `/\`) {
		return "", fmt.Errorf("unsafe ssh key file name %q in credentials secret", name)
	}
	return clean, nil
}

// writeSecretFile writes secret material with an explicit permission, ignoring
// the process umask, and returns an error that never contains the content.
func writeSecretFile(path string, data []byte, mode os.FileMode) error {
	if err := os.WriteFile(path, data, mode); err != nil {
		return fmt.Errorf("write git credential file %q: %w", path, err)
	}
	if err := os.Chmod(path, mode); err != nil {
		return fmt.Errorf("set permissions on git credential file %q: %w", path, err)
	}
	return nil
}

// gitRepositoryHost extracts the host from an HTTPS/SSH git URL. It also accepts
// scp-like SSH URLs (git@github.com:org/repo.git). The port is dropped: GitHub
// (and the OCI hosts in use) serve git on the default ports.
func gitRepositoryHost(repositoryURL string) string {
	raw := strings.TrimSpace(repositoryURL)
	if raw == "" {
		return ""
	}

	if parsed, err := url.Parse(raw); err == nil && parsed.Host != "" {
		return parsed.Hostname()
	}

	// scp-like: [user@]host:path
	if at := strings.LastIndex(raw, "@"); at >= 0 {
		rest := raw[at+1:]
		if colon := strings.Index(rest, ":"); colon > 0 {
			return rest[:colon]
		}
	}

	return ""
}

func hasAnyDataKey(secret *corev1.Secret, keys ...string) bool {
	for _, key := range keys {
		if _, ok := secret.Data[key]; ok {
			return true
		}
	}
	return false
}
