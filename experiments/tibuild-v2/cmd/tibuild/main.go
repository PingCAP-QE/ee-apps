package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/signal"
	"sync"
	"syscall"

	"github.com/rs/zerolog"
	"goa.design/clue/debug"
	"goa.design/clue/log"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/impl"
)

func main() {
	// Define command line flags, add any other flag required to configure the
	// service.
	var (
		hostF      = flag.String("host", "development", "Server host (valid values: development, product)")
		domainF    = flag.String("domain", "", "Host domain name (overrides host domain specified in service design)")
		httpPortF  = flag.String("http-port", "", "HTTP port (overrides host HTTP port specified in service design)")
		secureF    = flag.Bool("secure", false, "Use secure scheme (https or grpcs)")
		dbgF       = flag.Bool("debug", false, "Log request and response bodies")
		configFile = flag.String("config", "config.yaml", "Path to config file")
	)
	flag.Parse()

	// Setup logger. Replace logger with your own log package of choice.
	format := log.FormatJSON
	if log.IsTerminal() {
		format = log.FormatTerminal
	}
	ctx := log.Context(context.Background(), log.WithFormat(format))
	if *dbgF {
		ctx = log.Context(ctx, log.WithDebug())
		log.Debugf(ctx, "debug logs enabled")
	}
	log.Print(ctx, log.KV{K: "http-port", V: *httpPortF})

	// Load and parse configuration
	cfg, err := loadConfig(*configFile)
	if err != nil {
		log.Fatalf(ctx, err, "failed to load configuration")
	}

	// Initialize the services.
	var (
		artifactSvc artifact.Service
		devbuildSvc devbuild.Service
	)
	{
		logLevel := zerolog.InfoLevel
		if *dbgF {
			logLevel = zerolog.DebugLevel
		}
		zerolog.SetGlobalLevel(logLevel)
		{
			logger := zerolog.New(os.Stderr).With().Timestamp().Str("service", artifact.ServiceName).Logger()
			artifactSvc = impl.NewArtifact(&logger)
		}
		{
			dbClient, err := newStoreClient(cfg)
			if err != nil {
				log.Fatalf(ctx, err, "failed to create store client")
			}
			logger := zerolog.New(os.Stderr).With().Timestamp().Str("service", devbuild.ServiceName).Logger()
			devbuildSvc = impl.NewDevbuild(&logger, dbClient)
		}
	}

	// Wrap the services in endpoints that can be invoked from other services
	// potentially running in different processes.
	var (
		artifactEndpoints *artifact.Endpoints
		devbuildEndpoints *devbuild.Endpoints
	)
	{
		artifactEndpoints = artifact.NewEndpoints(artifactSvc)
		artifactEndpoints.Use(debug.LogPayloads())
		artifactEndpoints.Use(log.Endpoint)
		devbuildEndpoints = devbuild.NewEndpoints(devbuildSvc)
		devbuildEndpoints.Use(debug.LogPayloads())
		devbuildEndpoints.Use(log.Endpoint)
	}

	// Create channel used by both the signal handler and server goroutines
	// to notify the main goroutine when to stop the server.
	errc := make(chan error)

	// Setup interrupt handler. This optional step configures the process so
	// that SIGINT and SIGTERM signals cause the services to stop gracefully.
	go func() {
		c := make(chan os.Signal, 1)
		signal.Notify(c, syscall.SIGINT, syscall.SIGTERM)
		errc <- fmt.Errorf("%s", <-c)
	}()

	var wg sync.WaitGroup
	ctx, cancel := context.WithCancel(ctx)

	// Start the servers and send errors (if any) to the error channel.
	switch *hostF {
	case "development":
		{
			addr := "http://localhost:8080"
			u, err := url.Parse(addr)
			if err != nil {
				log.Fatalf(ctx, err, "invalid URL %#v\n", addr)
			}
			if *secureF {
				u.Scheme = "https"
			}
			if *domainF != "" {
				u.Host = *domainF
			}
			if *httpPortF != "" {
				h, _, err := net.SplitHostPort(u.Host)
				if err != nil {
					log.Fatalf(ctx, err, "invalid URL %#v\n", u.Host)
				}
				u.Host = net.JoinHostPort(h, *httpPortF)
			} else if u.Port() == "" {
				u.Host = net.JoinHostPort(u.Host, "80")
			}
			handleHTTPServer(ctx, u, artifactEndpoints, devbuildEndpoints, &wg, errc, *dbgF)
		}

	case "product":
		{
			addr := "http://0.0.0.0:8080"
			u, err := url.Parse(addr)
			if err != nil {
				log.Fatalf(ctx, err, "invalid URL %#v\n", addr)
			}
			if *secureF {
				u.Scheme = "https"
			}
			if *domainF != "" {
				u.Host = *domainF
			}
			if *httpPortF != "" {
				h, _, err := net.SplitHostPort(u.Host)
				if err != nil {
					log.Fatalf(ctx, err, "invalid URL %#v\n", u.Host)
				}
				u.Host = net.JoinHostPort(h, *httpPortF)
			} else if u.Port() == "" {
				u.Host = net.JoinHostPort(u.Host, "80")
			}
			handleHTTPServer(ctx, u, artifactEndpoints, devbuildEndpoints, &wg, errc, *dbgF)
		}

	default:
		log.Fatal(ctx, fmt.Errorf("invalid host argument: %q (valid hosts: development|product)", *hostF))
	}

	// Wait for signal.
	log.Printf(ctx, "exiting (%v)", <-errc)

	// Send cancellation signal to the goroutines.
	cancel()

	wg.Wait()
	log.Printf(ctx, "exited")
}
