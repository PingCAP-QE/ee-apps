package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/config"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/decision"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/extauth"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/identity"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/observability"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/openfga"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/registry"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/resolver"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"google.golang.org/grpc"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(logger); err != nil {
		logger.Error("authzd stopped", "error", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return runContext(ctx, logger, os.Getenv)
}

func runContext(ctx context.Context, logger *slog.Logger, getenv func(string) string) error {
	cfg, err := config.Load(getenv)
	if err != nil {
		return err
	}
	registryData, err := os.ReadFile(cfg.RegistryPath)
	if err != nil {
		return errors.New("could not read authorization registry")
	}
	policyRegistry, err := registry.Parse(registryData)
	if err != nil {
		return fmt.Errorf("authorization registry is invalid: %w", err)
	}
	resourceResolver, err := loadResolver(cfg.ResourceCatalog)
	if err != nil {
		return err
	}
	checker, err := openfga.NewChecker(openfga.Config{
		APIURL: cfg.OpenFGAURL, StoreID: cfg.OpenFGAStoreID,
		ModelID: cfg.OpenFGAModelID, APIToken: cfg.OpenFGAAPIToken,
	})
	if err != nil {
		return fmt.Errorf("OpenFGA configuration is invalid: %w", err)
	}
	verifiers := make(map[string]decision.Credential, len(cfg.Credentials))
	var closers []*identity.JWTVerifier
	defer func() {
		for _, verifier := range closers {
			_ = verifier.Close(context.Background())
		}
	}()
	for mode, credential := range cfg.Credentials {
		verifier, err := identity.NewJWTVerifier(ctx, identity.JWTConfig{
			JWKSURL: credential.JWKSURL, Issuer: credential.Issuer, Audience: credential.Audience,
			PrincipalPrefix: credential.PrincipalPrefix, SubjectClaim: credential.SubjectClaim,
		})
		if err != nil {
			return fmt.Errorf("credential mode %q could not initialize its JWKS verifier", mode)
		}
		closers = append(closers, verifier)
		verifiers[mode] = decision.Credential{Header: credential.Header, Verifier: verifier}
	}
	audit := observability.NewRecorder(logger)
	authorizer := decision.NewAuthorizer(policyRegistry, verifiers, resourceResolver, checker, audit)
	grpcListener, err := net.Listen("tcp", cfg.GRPCAddress)
	if err != nil {
		return fmt.Errorf("listen for ext_authz: %w", err)
	}
	privateListener, err := net.Listen("tcp", cfg.PrivateAddress)
	if err != nil {
		_ = grpcListener.Close()
		return fmt.Errorf("listen for private health and metrics: %w", err)
	}
	grpcServer := grpc.NewServer()
	authv3.RegisterAuthorizationServer(grpcServer, extauth.NewServer(authorizer))
	var ready atomic.Bool
	ready.Store(true)
	privateServer := &http.Server{
		Handler: audit.Handler(ready.Load), ReadHeaderTimeout: 5 * time.Second,
	}
	errorsFromServers := make(chan error, 2)
	go func() { errorsFromServers <- grpcServer.Serve(grpcListener) }()
	go func() { errorsFromServers <- privateServer.Serve(privateListener) }()
	logger.Info("authzd listeners started", "grpc_address", cfg.GRPCAddress, "private_address", cfg.PrivateAddress)
	select {
	case <-ctx.Done():
	case err := <-errorsFromServers:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			ready.Store(false)
			grpcServer.Stop()
			_ = privateServer.Close()
			return err
		}
	}
	ready.Store(false)
	grpcServer.GracefulStop()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return privateServer.Shutdown(shutdownCtx)
}

func loadResolver(catalogPath string) (decision.Resolver, error) {
	if strings.TrimSpace(catalogPath) == "" {
		return resolver.Unavailable{}, nil
	}
	data, err := os.ReadFile(catalogPath)
	if err != nil {
		return nil, errors.New("could not read trusted resource catalog")
	}
	catalog, err := resolver.Parse(data)
	if err != nil {
		return nil, fmt.Errorf("trusted resource catalog is invalid: %w", err)
	}
	return catalog, nil
}
