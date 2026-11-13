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

	gcs "github.com/PingCAP-QE/ee-apps/dl/gen/gcs"
	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
	gcssvc "github.com/PingCAP-QE/ee-apps/dl/internal/service/gcs"
	ocisvc "github.com/PingCAP-QE/ee-apps/dl/internal/service/oci"
	"github.com/PingCAP-QE/ee-apps/dl/pkg/reload"
)

func main() {
	// Define command line flags, add any other flag required to configure the
	// service.
	var (
		hostF       = flag.String("host", "localhost", "Server host (valid values: localhost)")
		domainF     = flag.String("domain", "", "Host domain name (overrides host domain specified in service design)")
		httpPortF   = flag.String("http-port", "", "HTTP port (overrides host HTTP port specified in service design)")
		secureF     = flag.Bool("secure", false, "Use secure scheme (https or grpcs)")
		dbgF        = flag.Bool("debug", false, "Log request and response bodies")
		ociCfgPathF = flag.String("oci-config", "oci.yaml", "oci config yaml file path")
		// Deprecated: the KS3 backend is gone. The flag stays accepted for one
		// release so the new image can be rolled out while the deployment still
		// passes it — `flag.Parse` exits on an unknown flag, so dropping it here
		// would turn the rollout order into a crash loop instead of a no-op.
		_           = flag.String("ks3-config", "ks3.yaml", "deprecated: the KS3 backend was removed, this flag is ignored")
		gcsCfgPathF = flag.String("gcs-config", "", "gcs config file path (yaml or json, optional: uses ADC if not set)")

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

	// Initialize the services.
	var (
		ociSvc oci.Service
		gcsSvc gcs.Service
	)
	{
		ociSvc = ocisvc.New(logger, ociCfgPathF, splitCommaList(*ociAllowedRegistriesF))
		gcsSvc = gcssvc.New(logger, *gcsCfgPathF, splitCommaList(*gcsAllowedBucketsF), splitCommaList(*gcsAllowedPrefixesF))
	}

	// Wrap the services in endpoints that can be invoked from other services
	// potentially running in different processes.
	var (
		ociEndpoints *oci.Endpoints
		gcsEndpoints *gcs.Endpoints
	)
	{
		ociEndpoints = oci.NewEndpoints(ociSvc)
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
			handleHTTPServer(ctx, u, ociEndpoints, gcsEndpoints, &wg, errc, logger, *dbgF, ociSvc)
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
