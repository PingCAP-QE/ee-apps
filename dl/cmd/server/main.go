package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	gcs "github.com/PingCAP-QE/ee-apps/dl/gen/gcs"
	ks3 "github.com/PingCAP-QE/ee-apps/dl/gen/ks3"
	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
	"github.com/PingCAP-QE/ee-apps/dl/internal/service/delivery"
	gcssvc "github.com/PingCAP-QE/ee-apps/dl/internal/service/gcs"
	ks3svc "github.com/PingCAP-QE/ee-apps/dl/internal/service/ks3"
	ocisvc "github.com/PingCAP-QE/ee-apps/dl/internal/service/oci"
	"github.com/PingCAP-QE/ee-apps/dl/pkg/reload"
)

func main() {
	// Define command line flags, add any other flag required to configure the
	// service.
	var (
		hostF               = flag.String("host", "localhost", "Server host (valid values: localhost)")
		domainF             = flag.String("domain", "", "Host domain name (overrides host domain specified in service design)")
		httpPortF           = flag.String("http-port", "", "HTTP port (overrides host HTTP port specified in service design)")
		secureF             = flag.Bool("secure", false, "Use secure scheme (https or grpcs)")
		dbgF                = flag.Bool("debug", false, "Log request and response bodies")
		ks3CfgPathF         = flag.String("ks3-config", "ks3.yaml", "ks3 config yaml file path")
		ociCfgPathF         = flag.String("oci-config", "oci.yaml", "oci config yaml file path")
		gcsCfgPathF         = flag.String("gcs-config", "", "gcs config file path (yaml or json, optional: uses ADC if not set)")
		modeF               = flag.String("server-mode", "internal", "Server surface: internal, management, public, or delivery-migrate")
		deliveryConfigF     = flag.String("delivery-config", "", "Delivery issuer policies and HTTPS public origin (YAML)")
		deliveryHealthF     = flag.String("delivery-health-address", ":8001", "Separate private delivery probe listener; never expose through the public route")
		deliveryConcurrentF = flag.Int("delivery-concurrency", 32, "Maximum concurrent delivery requests per replica")
		deliveryRPSF        = flag.Float64("delivery-rps", 10, "Maximum delivery requests per second per replica")
		deliveryBurstF      = flag.Int("delivery-burst", 20, "Delivery request rate burst per replica")
		deliveryTimeoutF    = flag.Duration("delivery-transfer-timeout", 24*time.Hour, "Maximum duration of an authorized transfer")

		ociAllowedRegistriesF = flag.String("oci-allowed-registries", "", "comma-separated registry hosts allowed for OCI downloads; empty denies all")
		gcsAllowedBucketsF    = flag.String("gcs-allowed-buckets", "", "comma-separated GCS buckets allowed for object downloads; empty denies all")
		gcsAllowedPrefixesF   = flag.String("gcs-allowed-prefixes", "", "optional comma-separated object key prefixes allowed for GCS downloads")
	)
	flag.Parse()

	// Setup logger. Replace logger with your own log package of choice.
	var (
		logger *log.Logger
	)
	{
		logger = log.New(os.Stderr, "[dl] ", log.Ltime)
	}
	if *modeF != "internal" {
		if *dbgF {
			logger.Fatal("debug logging is incompatible with delivery modes")
		}
		address := "localhost:8000"
		if *domainF != "" {
			address = *domainF
		}
		if *httpPortF != "" {
			host, _, err := net.SplitHostPort(address)
			if err != nil {
				logger.Fatal("invalid delivery listen address")
			}
			address = net.JoinHostPort(host, *httpPortF)
		}
		if err := runDelivery(*modeF, address, *deliveryHealthF, *deliveryConfigF, ociCfgPathF, splitCommaList(*ociAllowedRegistriesF), delivery.Limits{Concurrent: *deliveryConcurrentF, RequestsPerSecond: *deliveryRPSF, Burst: *deliveryBurstF, TransferTimeout: *deliveryTimeoutF}, logger); err != nil {
			logger.Fatal(err)
		}
		return
	}

	// Initialize the services.
	var (
		ociSvc oci.Service
		ks3Svc ks3.Service
		gcsSvc gcs.Service
	)
	{
		ociSvc = ocisvc.New(logger, ociCfgPathF, splitCommaList(*ociAllowedRegistriesF))
		ks3Svc = ks3svc.New(logger, *ks3CfgPathF)
		gcsSvc = gcssvc.New(logger, *gcsCfgPathF, splitCommaList(*gcsAllowedBucketsF), splitCommaList(*gcsAllowedPrefixesF))
	}

	// Wrap the services in endpoints that can be invoked from other services
	// potentially running in different processes.
	var (
		ociEndpoints *oci.Endpoints
		ks3Endpoints *ks3.Endpoints
		gcsEndpoints *gcs.Endpoints
	)
	{
		ociEndpoints = oci.NewEndpoints(ociSvc)
		ks3Endpoints = ks3.NewEndpoints(ks3Svc)
		gcsEndpoints = gcs.NewEndpoints(gcsSvc)
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
	ctx, cancel := context.WithCancel(context.Background())

	// Start config file watchers for hot reload.
	{
		type reloader interface {
			Reload(ctx context.Context, cfgFile string) error
		}
		if r, ok := gcsSvc.(reloader); ok {
			go reload.WatchFile(ctx, *gcsCfgPathF, logger, func() error {
				return r.Reload(ctx, *gcsCfgPathF)
			})
		}
		if r, ok := ks3Svc.(reloader); ok {
			go reload.WatchFile(ctx, *ks3CfgPathF, logger, func() error {
				return r.Reload(ctx, *ks3CfgPathF)
			})
		}
		if r, ok := ociSvc.(reloader); ok {
			go reload.WatchFile(ctx, *ociCfgPathF, logger, func() error {
				return r.Reload(ctx, *ociCfgPathF)
			})
		}
	}

	// Start the servers and send errors (if any) to the error channel.
	switch *hostF {
	case "localhost":
		{
			addr := "http://localhost:8000"
			u, err := url.Parse(addr)
			if err != nil {
				logger.Fatalf("invalid URL %#v: %s\n", addr, err)
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
					logger.Fatalf("invalid URL %#v: %s\n", u.Host, err)
				}
				u.Host = net.JoinHostPort(h, *httpPortF)
			} else if u.Port() == "" {
				u.Host = net.JoinHostPort(u.Host, "80")
			}
			handleHTTPServer(ctx, u, ociEndpoints, ks3Endpoints, gcsEndpoints, &wg, errc, logger, *dbgF, ociSvc)
		}

	default:
		logger.Fatalf("invalid host argument: %q (valid hosts: localhost)\n", *hostF)
	}

	// Wait for signal.
	logger.Printf("exiting (%v)", <-errc)

	// Send cancellation signal to the goroutines.
	cancel()

	wg.Wait()
	logger.Println("exited")
}

func splitCommaList(value string) []string {
	var out []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}
