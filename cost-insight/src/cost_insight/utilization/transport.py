"""Existing Tencent Kafka transport, explicit TLS/SCRAM identity; no topic creation."""

import json
import os


def _config():
    names = (
        "UTILIZATION_KAFKA_BOOTSTRAP_SERVERS",
        "UTILIZATION_KAFKA_USERNAME",
        "UTILIZATION_KAFKA_PASSWORD",
        "UTILIZATION_KAFKA_CA_FILE",
    )
    if any(not os.environ.get(name) for name in names):
        raise ValueError("explicit Tencent Kafka TLS/SCRAM configuration is required")
    return {
        "bootstrap_servers": os.environ[names[0]].split(","),
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "SCRAM-SHA-512",
        "sasl_plain_username": os.environ[names[1]],
        "sasl_plain_password": os.environ[names[2]],
        "ssl_cafile": os.environ[names[3]],
        "request_timeout_ms": 20000,
    }


class Producer:
    def __init__(self):
        from kafka import KafkaProducer

        self.topic = os.environ.get("UTILIZATION_KAFKA_TOPIC", "tencent-utilization-tasks")
        self.client = KafkaProducer(
            **_config(),
            acks="all",
            retries=0,
            value_serializer=lambda value: json.dumps(value).encode(),
        )

    def publish(self, message):
        self.client.send(self.topic, value=message).get(timeout=20)

    def close(self):
        self.client.close(timeout=20)


def consumer():
    from kafka import KafkaConsumer

    return KafkaConsumer(
        os.environ.get("UTILIZATION_KAFKA_TOPIC", "tencent-utilization-tasks"),
        **_config(),
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        group_id=os.environ.get("UTILIZATION_KAFKA_GROUP", "tencent-utilization-workers"),
        max_poll_interval_ms=600000,
    )
