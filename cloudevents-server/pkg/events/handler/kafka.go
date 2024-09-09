package handler

import (
	"context"
	"sync"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/rs/zerolog/log"
	kafka "github.com/segmentio/kafka-go"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	skakfa "github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/kafka"
)

func NewEventProducer(cfg config.Kafka) (*EventProducer, error) {
	writer, err := skakfa.NewWriter(cfg.Authentication, cfg.Brokers, "", cfg.ClientID)
	if err != nil {
		return nil, err
	}

	return &EventProducer{
		writer:           writer,
		unknowEventTopic: cfg.Producer.DefaultTopic,
		topicMapping:     cfg.Producer.TopicMapping,
	}, nil
}

func NewEventConsumer(cfg config.Kafka, topic string, hander EventHandler, faultWriter *kafka.Writer) (*EventConsumer, error) {
	reader, err := skakfa.NewReader(cfg.Authentication, cfg.Brokers, topic, cfg.Consumer.GroupID, cfg.ClientID)
	if err != nil {
		return nil, err
	}

	return &EventConsumer{
		reader:     reader,
		handler:    hander,
		writer:     faultWriter,
		faultTopic: cfg.Consumer.DeadLetterTopic,
	}, nil
}

func NewEventConsumerGroup(cfg config.Kafka, hander EventHandler) (EventConsumerGroup, error) {
	faultWriter, err := skakfa.NewWriter(cfg.Authentication, cfg.Brokers, "", cfg.ClientID)
	if err != nil {
		return nil, err
	}

	consumerGroup := make(EventConsumerGroup)
	for _, topic := range cfg.Consumer.TopicMapping {
		if consumerGroup[topic] != nil {
			continue
		}
		consumer, err := NewEventConsumer(cfg, topic, hander, faultWriter)
		if err != nil {
			return nil, err
		}

		consumerGroup[topic] = consumer
	}

	return consumerGroup, nil
}

// EventProducer is the main structure for our event broker
type EventProducer struct {
	writer           *kafka.Writer
	unknowEventTopic string
	topicMapping     map[string]string // Map event type to Kafka topic
}

func (eb *EventProducer) HandleCloudEvent(ctx context.Context, event cloudevents.Event) cloudevents.Result {
	eventType := event.Type()
	topic, ok := eb.topicMapping[eventType]

	// Use default topic if not found in mapping
	if !ok {
		log.Debug().Str("event-type", eventType).Msg("No topic found for event type, using default topic")
		topic = eb.unknowEventTopic
	}

	cloudEventBytes, err := event.MarshalJSON()
	if err != nil {
		log.Err(err).Msg("error marshalling Cloud Event")
		return cloudevents.ResultNACK
	}

	message := kafka.Message{
		Topic: topic,
		Key:   []byte(event.ID()),
		Value: cloudEventBytes,
	}

	err = eb.writer.WriteMessages(ctx, message)
	if err != nil {
		log.Err(err).Str("topic", topic).Str("ce-id", event.ID()).Msg("error writing message to Kafka")
		return err
	}

	log.Debug().Str("topic", topic).Str("ce-id", event.ID()).Msg("message written to Kafka")
	return cloudevents.ResultACK
}

type EventConsumerGroup map[string]*EventConsumer

func (ecs EventConsumerGroup) Close() {
	for _, ec := range ecs {
		if ec != nil {
			ec.Close()
		}
	}
}

// Start runs the EventConsumerGroup in parallel, starting each EventConsumer
// in a separate goroutine. It waits for all EventConsumers to finish before
// returning.
func (ecs EventConsumerGroup) Start(ctx context.Context) {
	wg := new(sync.WaitGroup)
	for _, ec := range ecs {
		if ec != nil {
			wg.Add(1)
			go func(c *EventConsumer) {
				c.Start(ctx)
				wg.Done()
			}(ec)
		}
	}
	wg.Wait()
}

type EventConsumer struct {
	reader     *kafka.Reader
	writer     *kafka.Writer // used for ack and put into dead letter queue.
	handler    EventHandler
	faultTopic string // dead letter topic
}

// consumer workers
func (ec *EventConsumer) Start(ctx context.Context) error {
	defer ec.Close()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			m, err := ec.reader.ReadMessage(ctx)
			if err != nil {
				return err
			}

			var event cloudevents.Event
			err = event.UnmarshalJSON(m.Value)
			if err != nil {
				return err
			}

			result := ec.handler.Handle(event)
			if !cloudevents.IsACK(result) {
				log.Error().Err(err).Msg("error handling event")
				ec.writer.WriteMessages(ctx, kafka.Message{Topic: ec.faultTopic, Key: m.Key, Value: m.Value})
			}
		}
	}
}

func (ec *EventConsumer) Close() {
	if ec.reader != nil {
		ec.reader.Close()
	}
	if ec.writer != nil {
		ec.writer.Close()
	}
}
