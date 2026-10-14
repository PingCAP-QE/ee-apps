package impl

import (
	"io"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
)

func discardLogger() *zerolog.Logger {
	l := zerolog.New(io.Discard)
	return &l
}

func TestHasFailedLarkNotification(t *testing.T) {
	assert.False(t, hasFailedLarkNotification(schema.NotificationState{}))
	assert.False(t, hasFailedLarkNotification(schema.NotificationState{
		Lark: []schema.LarkMessageState{{ChatID: "", MessageID: "om_1"}},
	}))
	assert.True(t, hasFailedLarkNotification(schema.NotificationState{
		Lark: []schema.LarkMessageState{
			{ChatID: "", MessageID: "om_1"},
			{ChatID: "oc_x", Failed: true},
		},
	}))
}

func TestFilterFailedNotifications(t *testing.T) {
	builds := []*ent.DevBuild{
		{ID: 1, NotificationState: schema.NotificationState{
			Lark: []schema.LarkMessageState{{ChatID: "", MessageID: "om_1"}},
		}},
		{ID: 2, NotificationState: schema.NotificationState{
			Lark: []schema.LarkMessageState{{ChatID: "", Failed: true}},
		}},
		{ID: 3, NotificationState: schema.NotificationState{}},
		{ID: 4, NotificationState: schema.NotificationState{
			Lark: []schema.LarkMessageState{{ChatID: "oc_x", Failed: true}},
		}},
	}

	got := filterFailedNotifications(builds)
	assert.Len(t, got, 2)
	assert.Equal(t, 2, got[0].ID)
	assert.Equal(t, 4, got[1].ID)
}

func TestParseLarkRetryLookback(t *testing.T) {
	assert.Equal(t, defaultLarkRetryLookback, parseLarkRetryLookback("", discardLogger()))
	assert.Equal(t, 48*time.Hour, parseLarkRetryLookback("48h", discardLogger()))
	assert.Equal(t, defaultLarkRetryLookback, parseLarkRetryLookback("not-a-duration", discardLogger()))
	assert.Equal(t, defaultLarkRetryLookback, parseLarkRetryLookback("-1h", discardLogger()))
}
