package impl

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authn"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/authz"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/cache"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/config"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/githubapp"
	"github.com/PingCAP-QE/ee-apps/github-token-broker/internal/metrics"
	tokenapi "github.com/PingCAP-QE/ee-apps/github-token-broker/internal/service/gen/token"
)

// Token implements the generated token service.
type Token struct {
	config    config.Provider
	minter    githubapp.Minter
	cache     *cache.Cache
	metrics   *metrics.Metrics
	logger    *slog.Logger
	readiness func(context.Context) error
	now       func() time.Time
}

// TokenOptions are the dependencies of the token service.
type TokenOptions struct {
	Config    config.Provider
	Minter    githubapp.Minter
	Cache     *cache.Cache
	Metrics   *metrics.Metrics
	Logger    *slog.Logger
	Readiness func(context.Context) error
	Now       func() time.Time
}

// NewToken creates the token service implementation.
func NewToken(opts TokenOptions) *Token {
	logger := opts.Logger
	if logger == nil {
		logger = slog.Default()
	}
	now := opts.Now
	if now == nil {
		now = time.Now
	}
	return &Token{
		config:    opts.Config,
		minter:    opts.Minter,
		cache:     opts.Cache,
		metrics:   opts.Metrics,
		logger:    logger,
		readiness: opts.Readiness,
		now:       now,
	}
}

// Issue implements the token issue method.
func (s *Token) Issue(ctx context.Context, payload *tokenapi.TokenPayload) (*tokenapi.TokenResult, error) {
	cfg := s.config.Current()
	if cfg == nil {
		return nil, tokenapi.MakeNotReady(errors.New("configuration is not loaded"))
	}
	caller, ok := authn.CallerFromContext(ctx)
	if !ok {
		return nil, tokenapi.MakeUnauthorized(errors.New("caller is not authenticated"))
	}
	owner, repository, err := splitRepo(payload.Repo)
	if err != nil {
		return nil, tokenapi.MakeBadRequest(err)
	}
	if err := validateNarrowing(payload.Repositories, repository); err != nil {
		return nil, tokenapi.MakeBadRequest(err)
	}

	permissions, err := cfg.EffectivePolicy().Authorize(caller, owner, repository, payload.Repositories, payload.Permissions)
	if err != nil {
		if errors.Is(err, authz.ErrCallerNotAllowed) ||
			errors.Is(err, authz.ErrRepositoryNotAllowed) ||
			errors.Is(err, authz.ErrPermissionNotAllowed) {
			return nil, tokenapi.MakeForbidden(err)
		}
		return nil, tokenapi.MakeBadRequest(err)
	}

	s.cache.SetTTLMargin(cfg.Cache.TTLMargin)
	fallbackPAT := strings.TrimSpace(cfg.GitHub.FallbackPAT)
	repositories := normalizeRepositories(payload.Repositories)
	if len(repositories) == 0 && hasWrite(permissions) {
		// Never hand out an installation-wide write token by default.
		repositories = []string{repository}
	}

	key := cacheKey(owner, repository, repositories, permissions)
	token, err := s.cache.GetOrMint(ctx, key, func(ctx context.Context) (cache.Token, error) {
		start := time.Now()
		minted, err := s.minter.Mint(ctx, owner, repository, repositories, permissions)
		s.metrics.MintDuration(time.Since(start))
		if err != nil {
			if errors.Is(err, githubapp.ErrNoInstallation) && fallbackPAT != "" {
				return cache.Token{
					Value:     fallbackPAT,
					ExpiresAt: s.now().Add(10 * time.Minute),
					Source:    "pat",
				}, nil
			}
			return cache.Token{}, err
		}
		return cache.Token{
			Value:          minted.Value,
			ExpiresAt:      minted.ExpiresAt,
			InstallationID: minted.InstallationID,
			Source:         "app",
		}, nil
	})
	if err != nil {
		switch {
		case errors.Is(err, githubapp.ErrNoInstallation):
			return nil, tokenapi.MakeNotFound(err)
		case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
			return nil, tokenapi.MakeTimeout(err)
		default:
			return nil, tokenapi.MakeUpstream(err)
		}
	}

	s.metrics.Request(token.Source, true)
	s.logger.Info("issued github token",
		"namespace", caller.Namespace,
		"service_account", caller.ServiceAccount,
		"owner", owner,
		"repository", repository,
		"repositories", repositories,
		"permissions", permissions,
		"source", token.Source,
	)

	result := &tokenapi.TokenResult{
		Token:  token.Value,
		Source: token.Source,
	}
	if !token.ExpiresAt.IsZero() {
		expiresAt := token.ExpiresAt.UTC().Format(time.RFC3339)
		result.ExpiresAt = &expiresAt
	}
	if token.InstallationID != 0 {
		installationID := token.InstallationID
		result.InstallationID = &installationID
	}
	return result, nil
}

// Healthz implements the liveness check.
func (s *Token) Healthz(context.Context) (string, error) {
	return "ok", nil
}

// Readyz implements the readiness check.
func (s *Token) Readyz(ctx context.Context) (string, error) {
	if s.config == nil || s.config.Current() == nil {
		return "", tokenapi.MakeNotReady(errors.New("configuration is not loaded"))
	}
	if s.readiness != nil {
		if err := s.readiness(ctx); err != nil {
			return "", tokenapi.MakeNotReady(err)
		}
	}
	return "ready", nil
}

func splitRepo(repo string) (string, string, error) {
	parts := strings.Split(repo, "/")
	if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || strings.TrimSpace(parts[1]) == "" {
		return "", "", errors.New("repo must be owner/name")
	}
	return strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]), nil
}

func validateNarrowing(repositories []string, repository string) error {
	if len(repositories) == 0 {
		return nil
	}
	found := false
	for _, name := range repositories {
		name = strings.TrimSpace(name)
		if name == "" || strings.Contains(name, "/") {
			return errors.New("repositories must be repository names")
		}
		if strings.EqualFold(name, repository) {
			found = true
		}
	}
	if !found {
		return fmt.Errorf("repositories must include the target repository %q", repository)
	}
	return nil
}

func normalizeRepositories(repositories []string) []string {
	seen := map[string]struct{}{}
	out := make([]string, 0, len(repositories))
	for _, name := range repositories {
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		key := strings.ToLower(name)
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

func hasWrite(permissions map[string]string) bool {
	for _, level := range permissions {
		if level == "write" {
			return true
		}
	}
	return false
}

func cacheKey(owner, repository string, repositories []string, permissions map[string]string) string {
	permKeys := make([]string, 0, len(permissions))
	for name, level := range permissions {
		permKeys = append(permKeys, name+"="+level)
	}
	sort.Strings(permKeys)
	return strings.ToLower(owner) + "|" + strings.ToLower(repository) + "|" + strings.Join(repositories, ",") + "|" + strings.Join(permKeys, ",")
}
