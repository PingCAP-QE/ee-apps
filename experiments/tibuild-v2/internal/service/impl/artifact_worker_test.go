package impl

import (
	"context"
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

func TestStartHonorsLegacyReclaimSwitch(t *testing.T) {
	tests := []struct {
		name       string
		legacy     bool
		wantStatus string
	}{
		{name: "legacy reclaim enabled resets PROCESSING", legacy: true, wantStatus: TaskStatusPending},
		{name: "lease mode leaves PROCESSING untouched", legacy: false, wantStatus: TaskStatusProcessing},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ctx := context.Background()
			db := serverlessTestDB(t)
			task, err := db.ImageSyncTask.Create().
				SetSource("hub.pingcap.net/pingcap/tidb:v1.0.0-20260101").
				SetTarget("docker.io/pingcap/tidb:v1.0.0-20260101").
				SetStatus(TaskStatusProcessing).
				Save(ctx)
			require.NoError(t, err)

			logger := zerolog.Nop()
			legacy := tt.legacy
			s := &artifactsrvc{
				logger:      &logger,
				db:          db,
				pollingRate: time.Hour,
				workerCfg:   config.Worker{LegacyStartupReclaim: &legacy},
			}

			runCtx, cancel := context.WithCancel(ctx)
			done := make(chan struct{})
			go func() {
				defer close(done)
				s.Start(runCtx)
			}()

			// The reclaim happens before the first poll, so a short wait is
			// enough for the enabled case. The disabled case must stay
			// PROCESSING regardless.
			deadline := time.Now().Add(time.Second)
			for time.Now().Before(deadline) {
				got, err := db.ImageSyncTask.Get(ctx, task.ID)
				require.NoError(t, err)
				if got.Status == tt.wantStatus {
					break
				}
				time.Sleep(5 * time.Millisecond)
			}

			cancel()
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("Start did not return after cancellation")
			}

			got, err := db.ImageSyncTask.Get(ctx, task.ID)
			require.NoError(t, err)
			require.Equal(t, tt.wantStatus, got.Status)
		})
	}
}
