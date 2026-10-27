package gitcred

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSetupWritesConfigAndFragment(t *testing.T) {
	home := t.TempDir()
	result, err := Setup(SetupOptions{
		Home:                home,
		BrokerURL:           "http://broker.example",
		Permissions:         "contents=read;pull_requests=read",
		Hosts:               "github.com, github.example.com",
		FallbackCredentials: "/tekton/home/.git-credentials",
	})
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	if len(result.RequestedHosts) != 2 || result.RequestedPermCount != 2 {
		t.Errorf("result = %+v", result)
	}

	data, err := os.ReadFile(result.ConfigFile)
	if err != nil {
		t.Fatalf("read config: %v", err)
	}
	var decoded fileConfig
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("decode config: %v", err)
	}
	if decoded.BrokerURL != "http://broker.example" {
		t.Errorf("broker URL = %q", decoded.BrokerURL)
	}
	if decoded.TokenFile != defaultCallerTokenFile {
		t.Errorf("token file = %q", decoded.TokenFile)
	}
	if decoded.Permissions["contents"] != "read" || decoded.Permissions["pull_requests"] != "read" {
		t.Errorf("permissions = %v", decoded.Permissions)
	}
	if decoded.StateFile != result.StateFile || decoded.CacheDir != result.CacheDir {
		t.Errorf("state/cache = %q, %q", decoded.StateFile, decoded.CacheDir)
	}

	fragment, err := os.ReadFile(result.GitConfigFragment)
	if err != nil {
		t.Fatalf("read fragment: %v", err)
	}
	text := string(fragment)
	for _, want := range []string{
		`[credential "https://github.com"]`,
		`[credential "https://github.example.com"]`,
		"useHttpPath = true",
		"insteadOf = git@github.com:",
		"insteadOf = ssh://git@github.com/",
		result.CredentialHelper + " -config " + result.ConfigFile,
	} {
		if !strings.Contains(text, want) {
			t.Errorf("fragment missing %q:\n%s", want, text)
		}
	}
	if !strings.HasPrefix(text, "[credential") {
		t.Errorf("fragment starts with an unexpected entry:\n%s", text)
	}

	// The state file must exist and be empty so the caller can distinguish
	// "git never asked" from "broker minted".
	info, err := os.Stat(result.StateFile)
	if err != nil {
		t.Fatalf("stat state file: %v", err)
	}
	if info.Size() != 0 {
		t.Errorf("state file size = %d, want 0", info.Size())
	}
	for _, dir := range []string{result.CacheDir, filepath.Dir(result.CredentialHelper)} {
		if _, err := os.Stat(dir); err != nil {
			t.Errorf("missing directory %s: %v", dir, err)
		}
	}
}

func TestSetupRejectsInvalidOptions(t *testing.T) {
	tests := []struct {
		name string
		opts SetupOptions
	}{
		{name: "missing home", opts: SetupOptions{BrokerURL: "http://b", Permissions: "contents=read", Hosts: "github.com"}},
		{name: "missing broker", opts: SetupOptions{Home: "/tmp", Permissions: "contents=read", Hosts: "github.com"}},
		{name: "missing hosts", opts: SetupOptions{Home: "/tmp", BrokerURL: "http://b", Permissions: "contents=read"}},
		{name: "bad permissions", opts: SetupOptions{Home: "/tmp", BrokerURL: "http://b", Permissions: "contents", Hosts: "github.com"}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Setup(tc.opts); err == nil {
				t.Fatalf("Setup(%+v) succeeded, want error", tc.opts)
			}
		})
	}
}

func TestSetupConfigIsReadableByHelper(t *testing.T) {
	home := t.TempDir()
	result, err := Setup(SetupOptions{
		Home:        home,
		BrokerURL:   "http://broker.example",
		Permissions: "contents=read",
		Hosts:       "github.com",
	})
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	cfg, err := ConfigFromFile(result.ConfigFile)
	if err != nil {
		t.Fatalf("ConfigFromFile: %v", err)
	}
	if _, err := New(cfg); err != nil {
		t.Fatalf("New from setup config: %v", err)
	}
}

func TestSetupUsesSharedPermissions(t *testing.T) {
	home := t.TempDir()
	result, err := Setup(SetupOptions{
		Home:        home,
		BrokerURL:   "http://broker.example",
		Permissions: "contents=read",
		Hosts:       "github.com",
	})
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	// Tekton steps may run as different UIDs, so everything the helper needs
	// must be readable (and the state file writable) by every step.
	checks := []struct {
		path string
		want os.FileMode
	}{
		{filepath.Dir(result.ConfigFile), sharedDirPerm},
		{result.CacheDir, sharedDirPerm},
		{filepath.Dir(result.CredentialHelper), sharedDirPerm},
		{result.ConfigFile, sharedFilePerm},
		{result.GitConfigFragment, sharedFilePerm},
		{result.StateFile, sharedWritePerm},
	}
	for _, check := range checks {
		info, err := os.Stat(check.path)
		if err != nil {
			t.Fatalf("stat %s: %v", check.path, err)
		}
		if got := info.Mode().Perm(); got != check.want {
			t.Errorf("%s mode = %o, want %o", check.path, got, check.want)
		}
	}
}
