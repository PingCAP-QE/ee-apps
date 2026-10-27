package gitcred

import (
	"fmt"
	"os"
	"strings"
	"time"
)

// Environment variables understood by ConfigFromEnv.
const (
	EnvBrokerURL             = "GIT_AUTH_BROKER_URL"
	EnvCallerTokenFile       = "GIT_AUTH_BROKER_TOKEN_FILE"
	EnvCacheDir              = "GIT_AUTH_CACHE_DIR"
	EnvPermissions           = "GIT_AUTH_PERMISSIONS"
	EnvHosts                 = "GIT_AUTH_HOSTS"
	EnvFallbackCredentials   = "GIT_AUTH_FALLBACK_CREDENTIALS"
	EnvStateFile             = "GIT_AUTH_STATE_FILE"
	EnvTimeout               = "GIT_AUTH_TIMEOUT"
	defaultCallerTokenFile   = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	defaultBrokerServiceURL  = "http://github-token-broker.github-token-broker.svc"
	defaultCacheDirSuffix    = "git-credential-broker"
	permissionPairSeparator  = ";"
	permissionValueSeparator = "="
)

// ConfigFromEnv builds a Config from the process environment. It is separated
// from main so the parsing rules stay testable.
func ConfigFromEnv(getenv func(string) string) (Config, error) {
	cfg := Config{
		BrokerURL:               strings.TrimSpace(getenv(EnvBrokerURL)),
		TokenFile:               strings.TrimSpace(getenv(EnvCallerTokenFile)),
		CacheDir:                strings.TrimSpace(getenv(EnvCacheDir)),
		FallbackCredentialsFile: strings.TrimSpace(getenv(EnvFallbackCredentials)),
		StateFile:               strings.TrimSpace(getenv(EnvStateFile)),
	}
	if cfg.BrokerURL == "" {
		cfg.BrokerURL = defaultBrokerServiceURL
	}
	if cfg.TokenFile == "" {
		cfg.TokenFile = defaultCallerTokenFile
	}
	if cfg.CacheDir == "" {
		if home, err := os.UserCacheDir(); err == nil {
			cfg.CacheDir = home + string(os.PathSeparator) + defaultCacheDirSuffix
		}
	}
	if raw := strings.TrimSpace(getenv(EnvHosts)); raw != "" {
		for _, host := range strings.Split(raw, ",") {
			if host = strings.TrimSpace(host); host != "" {
				cfg.Hosts = append(cfg.Hosts, host)
			}
		}
	}
	if raw := strings.TrimSpace(getenv(EnvPermissions)); raw != "" {
		permissions, err := ParsePermissions(raw)
		if err != nil {
			return Config{}, err
		}
		cfg.Permissions = permissions
	}
	if raw := strings.TrimSpace(getenv(EnvTimeout)); raw != "" {
		timeout, err := time.ParseDuration(raw)
		if err != nil {
			return Config{}, fmt.Errorf("gitcred: parse %s: %w", EnvTimeout, err)
		}
		cfg.Timeout = timeout
	}
	return cfg, nil
}

// ParsePermissions parses a permission spec such as "contents=read".
func ParsePermissions(raw string) (map[string]string, error) {
	permissions := map[string]string{}
	for _, pair := range strings.Split(raw, permissionPairSeparator) {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		name, level, ok := strings.Cut(pair, permissionValueSeparator)
		name = strings.TrimSpace(name)
		level = strings.TrimSpace(level)
		if !ok || name == "" || level == "" {
			return nil, fmt.Errorf("gitcred: invalid permission %q, want name=level pairs separated by %q", pair, permissionPairSeparator)
		}
		if level != "read" && level != "write" {
			return nil, fmt.Errorf("gitcred: invalid permission level %q, want read or write", level)
		}
		permissions[name] = level
	}
	if len(permissions) == 0 {
		return nil, fmt.Errorf("gitcred: %s does not contain any permission", EnvPermissions)
	}
	return permissions, nil
}
