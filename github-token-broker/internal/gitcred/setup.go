package gitcred

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// SetupOptions describes the files the setup mode writes into a pod's shared
// home directory.
type SetupOptions struct {
	// Home is the shared home directory of the pod.
	Home string
	// BrokerURL is the token broker base URL.
	BrokerURL string
	// Permissions is a permission spec such as "contents=read".
	Permissions string
	// Hosts is a comma separated list of git hosts.
	Hosts string
	// FallbackCredentials, when non-empty, is a .git-credentials file used when
	// the broker denies a request.
	FallbackCredentials string
	// CallerTokenFile overrides the ServiceAccount token path.
	CallerTokenFile string
}

// SetupResult reports the files written by Setup.
type SetupResult struct {
	ConfigFile         string
	GitConfigFragment  string
	StateFile          string
	CacheDir           string
	CredentialHelper   string
	RequestedHosts     []string
	RequestedPermCount int
}

// Setup writes the helper configuration into the shared home so later steps in
// the same pod can authenticate git through the broker. It keeps all JSON and
// gitconfig rendering in Go, so the Tekton step needs no shell tooling beyond
// cp and mkdir.
func Setup(opts SetupOptions) (SetupResult, error) {
	if strings.TrimSpace(opts.Home) == "" {
		return SetupResult{}, fmt.Errorf("gitcred: setup home directory is required")
	}
	if strings.TrimSpace(opts.BrokerURL) == "" {
		return SetupResult{}, fmt.Errorf("gitcred: setup broker URL is required")
	}
	permissions, err := ParsePermissions(opts.Permissions)
	if err != nil {
		return SetupResult{}, err
	}
	hosts := splitList(opts.Hosts)
	if len(hosts) == 0 {
		return SetupResult{}, fmt.Errorf("gitcred: setup needs at least one host")
	}

	authDir := filepath.Join(opts.Home, ".git-auth")
	cacheDir := filepath.Join(authDir, "cache")
	binDir := filepath.Join(authDir, "bin")
	for _, dir := range []string{authDir, cacheDir, binDir} {
		if err := os.MkdirAll(dir, cacheDirPerm); err != nil {
			return SetupResult{}, fmt.Errorf("gitcred: create %s: %w", dir, err)
		}
	}

	result := SetupResult{
		ConfigFile:         filepath.Join(authDir, "config.json"),
		GitConfigFragment:  filepath.Join(authDir, "gitconfig.fragment"),
		StateFile:          filepath.Join(authDir, "source"),
		CacheDir:           cacheDir,
		CredentialHelper:   filepath.Join(binDir, "git-credential"),
		RequestedHosts:     hosts,
		RequestedPermCount: len(permissions),
	}

	callerTokenFile := opts.CallerTokenFile
	if callerTokenFile == "" {
		callerTokenFile = defaultCallerTokenFile
	}
	config := fileConfig{
		BrokerURL:               opts.BrokerURL,
		TokenFile:               callerTokenFile,
		CacheDir:                cacheDir,
		Permissions:             permissions,
		Hosts:                   hosts,
		FallbackCredentialsFile: opts.FallbackCredentials,
		StateFile:               result.StateFile,
	}
	encoded, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return SetupResult{}, fmt.Errorf("gitcred: encode config: %w", err)
	}
	if err := os.WriteFile(result.ConfigFile, append(encoded, '\n'), statePerm); err != nil {
		return SetupResult{}, fmt.Errorf("gitcred: write config: %w", err)
	}

	if err := os.WriteFile(result.GitConfigFragment, []byte(gitConfigFragment(result.CredentialHelper, result.ConfigFile, hosts)), statePerm); err != nil {
		return SetupResult{}, fmt.Errorf("gitcred: write git config fragment: %w", err)
	}
	if err := os.WriteFile(result.StateFile, nil, statePerm); err != nil {
		return SetupResult{}, fmt.Errorf("gitcred: write state file: %w", err)
	}
	return result, nil
}

// gitConfigFragment scopes the helper to the requested hosts and rewrites SSH
// submodule URLs to HTTPS, which an installation token can authenticate.
// useHttpPath is required: without it git omits the repository path from the
// helper request, and the helper could only mint installation-wide tokens.
func gitConfigFragment(helperPath, configPath string, hosts []string) string {
	var b strings.Builder
	for _, host := range hosts {
		fmt.Fprintf(&b, "[credential \"https://%s\"]\n", host)
		b.WriteString("\thelper =\n")
		fmt.Fprintf(&b, "\thelper = %s -config %s\n", helperPath, configPath)
		b.WriteString("\tuseHttpPath = true\n")
	}
	b.WriteString("[url \"https://github.com/\"]\n")
	b.WriteString("\tinsteadOf = git@github.com:\n")
	b.WriteString("\tinsteadOf = ssh://git@github.com/\n")
	return b.String()
}

func splitList(raw string) []string {
	var out []string
	for _, item := range strings.Split(raw, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}
