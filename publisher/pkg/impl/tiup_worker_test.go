package impl

import (
	"errors"
	"testing"
	"time"

	"github.com/go-redis/redis/v8"
	"github.com/rs/zerolog"
)

func Test_tiupWorker_notifyLark(t *testing.T) {
	t.Skipf("It is manually test case")
	const testWebhookURL = "https://open.feishu.cn/open-apis/bot/v2/hook/<please-replace-it>"

	type fields struct {
		logger      zerolog.Logger
		redisClient *redis.Client
		options     struct {
			LarkWebhookURL   string
			MirrorURL        string
			PublicServiceURL string
			NightlyInterval  time.Duration
		}
	}
	type args struct {
		req *PublishRequestTiUP
		err error
	}
	tests := []struct {
		name   string
		fields fields
		args   args
	}{
		{
			name: "Empty webhook URL",
			fields: fields{
				logger: zerolog.New(nil),
				options: struct {
					LarkWebhookURL   string
					MirrorURL        string
					PublicServiceURL string
					NightlyInterval  time.Duration
				}{
					LarkWebhookURL: "",
				},
			},
			args: args{
				req: &PublishRequestTiUP{
					Publish: PublishInfoTiUP{
						Name:    "test-package",
						Version: "v1.0.0",
					},
				},
				err: errors.New("test error"),
			},
		},
		{
			name: "Valid notification with all fields",
			fields: fields{
				logger: zerolog.New(nil),
				options: struct {
					LarkWebhookURL   string
					MirrorURL        string
					PublicServiceURL string
					NightlyInterval  time.Duration
				}{
					LarkWebhookURL:   testWebhookURL,
					MirrorURL:        "https://test-mirror.com",
					PublicServiceURL: "https://test-service.com",
				},
			},
			args: args{
				req: &PublishRequestTiUP{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "test-repo",
							Tag:  "test-tag",
						},
					},
					Publish: PublishInfoTiUP{
						Name:    "test-package",
						Version: "v1.0.0",
						OS:      "linux",
						Arch:    "amd64",
					},
				},
				err: errors.New("publish failed"),
			},
		},
		{
			name: "Invalid webhook URL",
			fields: fields{
				logger: zerolog.New(nil),
				options: struct {
					LarkWebhookURL   string
					MirrorURL        string
					PublicServiceURL string
					NightlyInterval  time.Duration
				}{
					LarkWebhookURL: "invalid-url",
				},
			},
			args: args{
				req: &PublishRequestTiUP{
					Publish: PublishInfoTiUP{
						Name:    "test-package",
						Version: "v1.0.0",
					},
				},
				err: errors.New("test error"),
			},
		},
		{
			name: "Missing publish info",
			fields: fields{
				logger: zerolog.New(nil),
				options: struct {
					LarkWebhookURL   string
					MirrorURL        string
					PublicServiceURL string
					NightlyInterval  time.Duration
				}{
					LarkWebhookURL: testWebhookURL,
				},
			},
			args: args{
				req: &PublishRequestTiUP{},
				err: errors.New("missing publish info"),
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := &tiupWorker{
				logger:      tt.fields.logger,
				redisClient: tt.fields.redisClient,
				options:     tt.fields.options,
			}
			p.notifyLark(tt.args.req, tt.args.err)
		})
	}
}
