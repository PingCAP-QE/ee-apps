package gitcred

import "testing"

func TestConfigFromEnvDefaults(t *testing.T) {
	cfg, err := ConfigFromEnv(func(string) string { return "" })
	if err != nil {
		t.Fatalf("ConfigFromEnv: %v", err)
	}
	if cfg.BrokerURL != defaultBrokerServiceURL {
		t.Errorf("broker URL = %q, want %q", cfg.BrokerURL, defaultBrokerServiceURL)
	}
	if cfg.TokenFile != defaultCallerTokenFile {
		t.Errorf("token file = %q, want %q", cfg.TokenFile, defaultCallerTokenFile)
	}
	if len(cfg.Permissions) != 0 {
		t.Errorf("permissions = %v, want empty (the helper defaults to contents: read)", cfg.Permissions)
	}
}

func TestConfigFromEnvOverrides(t *testing.T) {
	env := map[string]string{
		EnvBrokerURL:           " http://broker.example ",
		EnvCallerTokenFile:     "/tmp/sa.token",
		EnvCacheDir:            "/tmp/cache",
		EnvPermissions:         "contents=read; pull_requests=read",
		EnvHosts:               "github.com, github.example.com ",
		EnvFallbackCredentials: "/tmp/.git-credentials",
		EnvStateFile:           "/tmp/state",
		EnvTimeout:             "3s",
	}
	cfg, err := ConfigFromEnv(func(key string) string { return env[key] })
	if err != nil {
		t.Fatalf("ConfigFromEnv: %v", err)
	}
	if cfg.BrokerURL != "http://broker.example" {
		t.Errorf("broker URL = %q", cfg.BrokerURL)
	}
	if cfg.TokenFile != "/tmp/sa.token" || cfg.CacheDir != "/tmp/cache" {
		t.Errorf("paths = %q, %q", cfg.TokenFile, cfg.CacheDir)
	}
	if cfg.Permissions["contents"] != "read" || cfg.Permissions["pull_requests"] != "read" {
		t.Errorf("permissions = %v", cfg.Permissions)
	}
	if len(cfg.Hosts) != 2 || cfg.Hosts[0] != "github.com" || cfg.Hosts[1] != "github.example.com" {
		t.Errorf("hosts = %v", cfg.Hosts)
	}
	if cfg.FallbackCredentialsFile != "/tmp/.git-credentials" || cfg.StateFile != "/tmp/state" {
		t.Errorf("fallback/state = %q, %q", cfg.FallbackCredentialsFile, cfg.StateFile)
	}
	if cfg.Timeout.String() != "3s" {
		t.Errorf("timeout = %v", cfg.Timeout)
	}
}

func TestConfigFromEnvRejectsBadValues(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
	}{
		{name: "invalid permission pair", env: map[string]string{EnvPermissions: "contents"}},
		{name: "invalid permission level", env: map[string]string{EnvPermissions: "contents=admin"}},
		{name: "empty permission list", env: map[string]string{EnvPermissions: " ; "}},
		{name: "invalid timeout", env: map[string]string{EnvTimeout: "soon"}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := ConfigFromEnv(func(key string) string { return tc.env[key] }); err == nil {
				t.Fatalf("ConfigFromEnv(%v) succeeded, want error", tc.env)
			}
		})
	}
}

func TestParsePermissions(t *testing.T) {
	tests := []struct {
		raw  string
		want map[string]string
	}{
		{raw: "contents=read", want: map[string]string{"contents": "read"}},
		{raw: "contents=read;issues=write", want: map[string]string{"contents": "read", "issues": "write"}},
		{raw: " contents = read ; ", want: map[string]string{"contents": "read"}},
	}
	for _, tc := range tests {
		got, err := ParsePermissions(tc.raw)
		if err != nil {
			t.Fatalf("ParsePermissions(%q): %v", tc.raw, err)
		}
		if len(got) != len(tc.want) {
			t.Fatalf("ParsePermissions(%q) = %v, want %v", tc.raw, got, tc.want)
		}
		for name, level := range tc.want {
			if got[name] != level {
				t.Errorf("ParsePermissions(%q)[%s] = %q, want %q", tc.raw, name, got[name], level)
			}
		}
	}
}
