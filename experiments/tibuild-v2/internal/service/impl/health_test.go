package impl

import (
	"context"
	"os"
	"testing"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
)

func TestHealthService(t *testing.T) {
	logger := zerolog.New(os.Stderr).With().Timestamp().Str("service", "health").Logger()
	healthSvc := NewHealth(&logger)

	t.Run("Healthz", func(t *testing.T) {
		ctx := context.Background()
		result, err := healthSvc.Healthz(ctx)
		assert.NoError(t, err)
		assert.True(t, result)
	})

	t.Run("Livez", func(t *testing.T) {
		ctx := context.Background()
		result, err := healthSvc.Livez(ctx)
		assert.NoError(t, err)
		assert.True(t, result)
	})
}
