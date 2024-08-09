package kafka

import (
	"crypto/tls"
	"time"

	"github.com/segmentio/kafka-go"
	"github.com/segmentio/kafka-go/sasl"
	"github.com/segmentio/kafka-go/sasl/plain"
	"github.com/segmentio/kafka-go/sasl/scram"
)

type Authentication struct {
	// Mechanism is the name of the SASL mechanism.
	// Possible values: "", "PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"  (defaults to "").
	Mechanism string `yaml:"mechanism,omitempty" json:"mechanism,omitempty"`
	// User is the authentication username for SASL/* authentication
	User string `yaml:"user,omitempty" json:"user,omitempty"`
	// User is the authentication password for SASL/* authentication
	Password string `yaml:"password,omitempty" json:"password,omitempty"`
}

// GetMechanism returns a SASL mechanism based on the provided mechanism type, username, and password.
//
// Parameters:
// - mechanismType: a string representing the type of SASL mechanism to use.
// - username: a string representing the username for authentication.
// - password: a string representing the password for authentication.
//
// Returns:
// - sasl.Mechanism: a SASL mechanism that can be used for authentication.
// - error: an error if the mechanism type is not supported or if there is an issue creating the mechanism.
func GetMechanism(auth Authentication) (sasl.Mechanism, error) {
	switch auth.Mechanism {
	case "PLAIN":
		return plain.Mechanism{Username: auth.User, Password: auth.Password}, nil
	case scram.SHA256.Name():
		return scram.Mechanism(scram.SHA256, auth.User, auth.Password)
	case scram.SHA512.Name():
		return scram.Mechanism(scram.SHA512, auth.User, auth.Password)
	}

	return nil, nil
}

// NewDialer creates a new kafka dialer with the specified mechanism and client ID.
//
// The mechanism parameter specifies the SASL mechanism to use for authentication.
// The clientID parameter specifies the client ID to use for the dialer.
// Returns a pointer to a kafka.Dialer and an error.
func NewDialer(mechanism sasl.Mechanism, clientID string) (*kafka.Dialer, error) {
	dialer := &kafka.Dialer{
		Timeout:       10 * time.Second,
		DualStack:     true,
		SASLMechanism: mechanism,
		ClientID:      clientID,
	}
	if mechanism != nil && mechanism.Name() != "PLAIN" {
		dialer.TLS = &tls.Config{MinVersion: tls.VersionTLS12}
	}

	return dialer, nil
}

// NewReaderConfig creates a new kafka reader configuration.
//
// Parameters:
// - brokers: a slice of strings representing the list of kafka brokers.
// - topics: a slice of strings representing the list of kafka topics.
// - dialer: a kafka.Dialer used for connection.
//
// Returns:
// - kafka.ReaderConfig: a kafka reader configuration.
func NewReaderConfig(brokers, topics []string, dialer *kafka.Dialer) kafka.ReaderConfig {
	ret := kafka.ReaderConfig{
		Brokers: brokers,
		Dialer:  dialer,
	}

	if len(topics) == 1 {
		ret.Topic = topics[0]
	}
	if len(topics) > 1 {
		ret.GroupTopics = topics
	}

	return ret
}

// NewWriterConfig returns a kafka.WriterConfig with the specified brokers, topic, and dialer.
//
// brokers is a list of Kafka brokers to connect to, topic is the Kafka topic to write to, and dialer is the dialer to use for connections.
// kafka.WriterConfig
func NewWriterConfig(brokers []string, topic string, dialer *kafka.Dialer) kafka.WriterConfig {
	return kafka.WriterConfig{
		Brokers: brokers,
		Dialer:  dialer,
		Topic:   topic,
	}
}
