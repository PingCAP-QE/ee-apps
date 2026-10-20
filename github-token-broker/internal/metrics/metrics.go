package metrics

import (
	"context"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel/attribute"
	otelprom "go.opentelemetry.io/otel/exporters/prometheus"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

// Metrics holds the broker's OpenTelemetry instruments and exposes them in
// Prometheus format. Swapping the exporter for OTLP is a local change.
type Metrics struct {
	registry    *prometheus.Registry
	provider    *sdkmetric.MeterProvider
	requests    metric.Int64Counter
	errors      metric.Int64Counter
	rateLimited metric.Int64Counter
	mintSeconds metric.Float64Histogram
}

// New creates the metrics instruments and a Prometheus registry.
func New() (*Metrics, error) {
	registry := prometheus.NewRegistry()
	exporter, err := otelprom.New(otelprom.WithRegisterer(registry))
	if err != nil {
		return nil, err
	}
	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(exporter))
	meter := provider.Meter("github-token-broker")

	requests, err := meter.Int64Counter(
		"github_token_broker_requests",
		metric.WithDescription("Total token requests by source and result."),
	)
	if err != nil {
		return nil, err
	}
	errorsCounter, err := meter.Int64Counter(
		"github_token_broker_errors",
		metric.WithDescription("Total token request errors by reason."),
	)
	if err != nil {
		return nil, err
	}
	rateLimited, err := meter.Int64Counter(
		"github_token_broker_rate_limited",
		metric.WithDescription("Total requests rejected by the rate limiter."),
	)
	if err != nil {
		return nil, err
	}
	mintSeconds, err := meter.Float64Histogram(
		"github_token_broker_mint_seconds",
		metric.WithDescription("Time spent minting GitHub App installation tokens."),
		metric.WithUnit("s"),
	)
	if err != nil {
		return nil, err
	}

	return &Metrics{
		registry:    registry,
		provider:    provider,
		requests:    requests,
		errors:      errorsCounter,
		rateLimited: rateLimited,
		mintSeconds: mintSeconds,
	}, nil
}

// Handler exposes the metrics in Prometheus format.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{})
}

// Request records a token request outcome. source may be empty for errors
// that happen before a token is issued.
func (m *Metrics) Request(source string, ok bool) {
	if m == nil {
		return
	}
	result := "ok"
	if !ok {
		result = "error"
	}
	m.requests.Add(context.Background(), 1, metric.WithAttributes(
		attribute.String("source", source),
		attribute.String("result", result),
	))
}

// Error records a failed request reason.
func (m *Metrics) Error(reason string) {
	if m == nil {
		return
	}
	m.errors.Add(context.Background(), 1, metric.WithAttributes(attribute.String("reason", reason)))
}

// RateLimited records a request rejected by the rate limiter.
func (m *Metrics) RateLimited() {
	if m == nil {
		return
	}
	m.rateLimited.Add(context.Background(), 1)
}

// MintDuration records how long a mint call took.
func (m *Metrics) MintDuration(d time.Duration) {
	if m == nil {
		return
	}
	m.mintSeconds.Record(context.Background(), d.Seconds())
}
