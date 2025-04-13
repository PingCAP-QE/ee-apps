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
	"time"

	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"
	"goa.design/clue/debug"
	"goa.design/clue/log"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/fileserver"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/image"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tiup"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl"
)

func main() {
	// Define command line flags, add any other flag required to configure the
	// service.
	var (
		hostF      = flag.String("host", "localhost", "Server host (valid values: localhost)")
		domainF    = flag.String("domain", "", "Host domain name (overrides host domain specified in service design)")
		httpPortF  = flag.String("http-port", "", "HTTP port (overrides host HTTP port specified in service design)")
		configFile = flag.String("config", "config.yaml", "Path to config file")
		secureF    = flag.Bool("secure", false, "Use secure scheme (https or grpcs)")
		dbgF       = flag.Bool("debug", false, "Log request and response bodies")
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

	// Setup logger.
	logLevel := zerolog.InfoLevel
	if *dbgF {
		logLevel = zerolog.DebugLevel
	}
	zerolog.SetGlobalLevel(logLevel)
	logger := zerolog.New(os.Stderr).With().Timestamp().Str("service", tiup.ServiceName).Logger()

	// Load and parse configuration
	config, err := loadConfig(*configFile)
	if err != nil {
		log.Fatalf(ctx, err, "failed to load configuration")
	}

	// Initialize the services.
	var (
		tiupSvc tiup.Service
		fsSvc   fileserver.Service
		imgSvc  image.Service
	)
	{
		// Configure Kafka kafkaWriter
		kafkaWriter := kafka.NewWriter(kafka.WriterConfig{
			Brokers:  config.Kafka.Brokers,
			Topic:    config.Kafka.Topic,
			Balancer: &kafka.LeastBytes{},
			Logger:   kafka.LoggerFunc(logger.Printf),
		})

		// Configure Redis client
		redisClient := redis.NewClient(&redis.Options{
			Addr:     config.Redis.Addr,
			Password: config.Redis.Password,
			Username: config.Redis.Username,
			DB:       config.Redis.DB,
		})

		tiupSvc = impl.NewTiup(&logger, kafkaWriter, redisClient, config.EventSource)
		fsSvc = impl.NewFileserver(&logger, kafkaWriter, redisClient, config.EventSource)
		imgSvc = impl.NewImage(&logger, redisClient, time.Hour)
	}

	// Wrap the services in endpoints that can be invoked from other services
	// potentially running in different processes.
	var (
		tiupEndpoints *tiup.Endpoints
		fsEndpoints   *fileserver.Endpoints
		imgEndpoints  *image.Endpoints
	)
	{
		tiupEndpoints = tiup.NewEndpoints(tiupSvc)
		tiupEndpoints.Use(debug.LogPayloads())
		tiupEndpoints.Use(log.Endpoint)
		fsEndpoints = fileserver.NewEndpoints(fsSvc)
		fsEndpoints.Use(debug.LogPayloads())
		fsEndpoints.Use(log.Endpoint)
		imgEndpoints = image.NewEndpoints(imgSvc)
		imgEndpoints.Use(debug.LogPayloads())
		imgEndpoints.Use(log.Endpoint)
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
	case "localhost":
		{
			addr := "http://0.0.0.0:80"
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
			handleHTTPServer(ctx, u, tiupEndpoints, fsEndpoints, &wg, errc, *dbgF)
		}

	default:
		log.Fatal(ctx, fmt.Errorf("invalid host argument: %q (valid hosts: localhost)", *hostF))
	}

	// Wait for signal.
	log.Printf(ctx, "exiting (%v)", <-errc)

	// Send cancellation signal to the goroutines.
	cancel()

	wg.Wait()
	log.Printf(ctx, "exited")
}
