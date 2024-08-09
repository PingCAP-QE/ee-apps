package kafka

import (
	"github.com/rs/zerolog/log"
	kafka "github.com/segmentio/kafka-go"
)

func NewReader(auth Authentication, brokers []string, topic, consumerGroupID, clientID string) (*kafka.Reader, error) {
	mechanism, err := GetMechanism(auth)
	if err != nil {
		return nil, err
	}
	dialer, err := NewDialer(mechanism, clientID)
	if err != nil {
		return nil, err
	}

	readerConfig := NewReaderConfig(brokers, []string{topic}, dialer)
	readerConfig.GroupID = consumerGroupID
	readerConfig.Logger = kafka.LoggerFunc(log.Printf)
	readerConfig.ErrorLogger = kafka.LoggerFunc(func(msg string, keysAndValues ...interface{}) {
		log.Error().Msgf(msg, keysAndValues...)
	})

	return kafka.NewReader(readerConfig), nil
}
