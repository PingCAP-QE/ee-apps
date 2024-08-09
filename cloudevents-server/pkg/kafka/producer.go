package kafka

import (
	"github.com/rs/zerolog/log"
	kafka "github.com/segmentio/kafka-go"
)

func NewWriter(auth Authentication, brokers []string, topic, clientID string) (*kafka.Writer, error) {
	mechanism, err := GetMechanism(auth)
	if err != nil {
		return nil, err
	}
	dialer, err := NewDialer(mechanism, clientID)
	if err != nil {
		return nil, err
	}

	writeConfig := kafka.WriterConfig{
		Brokers: brokers,
		Async:   true,
		Logger:  kafka.LoggerFunc(log.Printf),
		ErrorLogger: kafka.LoggerFunc(func(msg string, keysAndValues ...interface{}) {
			log.Error().Msgf(msg, keysAndValues...)
		}),
		Balancer: &kafka.Hash{},
		Dialer:   dialer,
		Topic:    topic,
	}

	writer := kafka.NewWriter(writeConfig)
	// create topic if not exist
	writer.AllowAutoTopicCreation = true

	return writer, nil
}
