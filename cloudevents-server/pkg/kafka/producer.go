package kafka

import (
	"time"

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
		ErrorLogger: kafka.LoggerFunc(func(msg string, keysAndValues ...any) {
			log.Error().Msgf(msg, keysAndValues...)
		}),
		WriteTimeout: 10 * time.Second,
		ReadTimeout:  10 * time.Second,
		BatchTimeout: 5 * time.Second,
		BatchSize:    10,
		Balancer:     &kafka.Hash{},
		Dialer:       dialer,
		Topic:        topic,
	}

	writer := kafka.NewWriter(writeConfig)
	// create topic if not exist
	writer.AllowAutoTopicCreation = true

	return writer, nil
}
