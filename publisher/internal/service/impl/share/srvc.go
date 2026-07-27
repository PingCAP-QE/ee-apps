package share

import (
	"sync"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/google/uuid"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

type BaseService struct {
	Logger *zerolog.Logger

	mu          sync.RWMutex
	kafkaWriter *kafka.Writer
	redisClient redis.Cmdable

	EventSource string
	StateTTL    time.Duration
}

// NewBaseServiceService returns a base service with Kafka and Redis clients.
func NewBaseServiceService(logger *zerolog.Logger, cfg config.Service) *BaseService {
	s := &BaseService{Logger: logger}
	s.initClients(cfg)
	return s
}

// NewBaseServiceForTest returns a base service with explicitly provided
// Kafka writer and Redis client. Intended for unit tests.
func NewBaseServiceForTest(logger *zerolog.Logger, writer *kafka.Writer, redisClient redis.Cmdable, eventSource string) *BaseService {
	return &BaseService{
		Logger:       logger,
		kafkaWriter:  writer,
		redisClient:  redisClient,
		EventSource:  eventSource,
		StateTTL:     DefaultStateTTL,
	}
}

// Writer returns the current Kafka writer in a thread-safe manner.
func (s *BaseService) Writer() *kafka.Writer {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.kafkaWriter
}

// Client returns the current Redis client in a thread-safe manner.
func (s *BaseService) Client() redis.Cmdable {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.redisClient
}

// Reload re-initializes Kafka and Redis clients from the given config.
func (s *BaseService) Reload(cfg config.Service) {
	s.initClients(cfg)
}

func (s *BaseService) initClients(cfg config.Service) {
	kafkaWriter := kafka.NewWriter(kafka.WriterConfig{
		Brokers:  cfg.Kafka.Brokers,
		Topic:    cfg.Kafka.Topic,
		Balancer: &kafka.LeastBytes{},
		Logger:   kafka.LoggerFunc(s.Logger.Printf),
	})

	redisClient := redis.NewClient(&redis.Options{
		Addr:     cfg.Redis.Addr,
		Password: cfg.Redis.Password,
		Username: cfg.Redis.Username,
		DB:       cfg.Redis.DB,
	})

	s.mu.Lock()
	oldWriter := s.kafkaWriter
	oldRedis := s.redisClient
	s.kafkaWriter = kafkaWriter
	s.redisClient = redisClient
	s.EventSource = cfg.EventSource
	s.StateTTL = DefaultStateTTL
	s.mu.Unlock()

	if oldWriter != nil {
		oldWriter.Close()
	}
	if oldRedis != nil {
		if c, ok := oldRedis.(*redis.Client); ok {
			c.Close()
		}
	}
}

func (s *BaseService) ComposeEvent(request any) cloudevents.Event {
	event := cloudevents.NewEvent()
	event.SetID(uuid.New().String())
	event.SetSource(s.EventSource)
	event.SetData(cloudevents.ApplicationJSON, request)
	return event
}
