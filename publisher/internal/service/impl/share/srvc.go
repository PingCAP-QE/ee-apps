package share

import (
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-redis/redis/v8"
	"github.com/google/uuid"
	"github.com/rs/zerolog"
	"github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

type BaseService struct {
	Logger      *zerolog.Logger
	KafkaWriter *kafka.Writer
	RedisClient redis.Cmdable
	EventSource string
	StateTTL    time.Duration
}

// NewService returns the tiup service implementation.
func NewBaseServiceService(logger *zerolog.Logger, cfg config.Service) *BaseService {
	// Configure Kafka kafkaWriter
	kafkaWriter := kafka.NewWriter(kafka.WriterConfig{
		Brokers:  cfg.Kafka.Brokers,
		Topic:    cfg.Kafka.Topic,
		Balancer: &kafka.LeastBytes{},
		Logger:   kafka.LoggerFunc(logger.Printf),
	})

	// Configure Redis client
	redisClient := redis.NewClient(&redis.Options{
		Addr:     cfg.Redis.Addr,
		Password: cfg.Redis.Password,
		Username: cfg.Redis.Username,
		DB:       cfg.Redis.DB,
	})

	return &BaseService{
		Logger:      logger,
		KafkaWriter: kafkaWriter,
		RedisClient: redisClient,
		EventSource: cfg.EventSource,
		StateTTL:    DefaultStateTTL,
	}
}

func (s *BaseService) ComposeEvent(request any) cloudevents.Event {
	event := cloudevents.NewEvent()
	event.SetID(uuid.New().String())
	event.SetSource(s.EventSource)
	event.SetData(cloudevents.ApplicationJSON, request)
	return event
}
