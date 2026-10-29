package impl

import (
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

func TestApplyConfigPollingPrecedence(t *testing.T) {
	tests := []struct {
		name       string
		imageSync  config.ImageSync
		worker     config.Worker
		wantPeriod time.Duration
	}{
		{
			name:       "legacy image_sync polling_rate is kept when worker is unset",
			imageSync:  config.ImageSync{PollingRate: "30s"},
			wantPeriod: 30 * time.Second,
		},
		{
			name:       "worker poll_interval wins over image_sync",
			imageSync:  config.ImageSync{PollingRate: "30s"},
			worker:     config.Worker{PollInterval: "2s"},
			wantPeriod: 2 * time.Second,
		},
		{
			name:       "default when nothing is set",
			wantPeriod: defaultPollingRate,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logger := zerolog.Nop()
			s := &artifactsrvc{logger: &logger}
			s.applyConfig(&config.Service{ImageSync: tt.imageSync, Worker: tt.worker})
			require.Equal(t, tt.wantPeriod, s.pollingRate)
		})
	}
}
