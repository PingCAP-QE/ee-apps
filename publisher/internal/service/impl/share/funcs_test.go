package share

import (
	"reflect"
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
)

func Test_xxx(t *testing.T) {
	if !cloudevents.IsACK(nil) {
		t.Errorf("IsACK(nil) = false, want true")
	}

	if !cloudevents.IsACK(cloudevents.NewReceipt(true, "xxxx")) {
		t.Errorf("xxx")
	}
	if !cloudevents.IsNACK(cloudevents.NewReceipt(true, "xxxx")) {
		t.Errorf("xxx")
	}

	if !cloudevents.IsNACK(cloudevents.NewReceipt(false, "xxxx")) {
		t.Errorf("xxx")
	}
}

func Test_fetchOCIArtifactConfig(t *testing.T) {
	type args struct {
		repo string
		tag  string
	}
	tests := []struct {
		name    string
		args    args
		want    map[string]any
		wantErr bool
	}{
		{
			name: "TestFetchOCIArtifactConfig",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb/package",
				tag:  "v8.3.0_linux_amd64",
			},
			want: map[string]any{
				"org.opencontainers.image.version": "v8.3.0-pre",
				"net.pingcap.tibuild.os":           "linux",
				"net.pingcap.tibuild.architecture": "amd64",
				"net.pingcap.tibuild.profile":      "release",
				"net.pingcap.tibuild.git-sha":      "1a0c3ac3292fff7742faa0c00a662ccb66ba40db",
				"net.pingcap.tibuild.tiup": []any{
					map[string]any{
						"description": "TiDB is an open source distributed HTAP database compatible with the MySQL protocol.",
						"entrypoint":  "tidb-server",
						"file":        "tidb-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]any{
						"description": "TiDB/TiKV cluster backup restore tool.",
						"entrypoint":  "br",
						"standalone":  true,
						"file":        "br-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]any{
						"description": "Dumpling is a CLI tool that helps you dump MySQL/TiDB data.",
						"entrypoint":  "dumpling",
						"file":        "dumpling-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]any{
						"description": "TiDB Lightning is a tool used for fast full import of large amounts of data into a TiDB cluster",
						"entrypoint":  "tidb-lightning",
						"standalone":  true,
						"file":        "tidb-lightning-v8.3.0-pre-linux-amd64.tar.gz",
					},
				},
			},
		},
		{
			name: "TestFetchOCIArtifactConfig",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb/package",
				tag:  "sha256:02ffdf71eaeca234cc4f03e8daaf19858e5f5ee8dcbecb38b98d5525851dacee",
			},
			want: map[string]any{
				"org.opencontainers.image.version": "v8.3.0-pre",
				"net.pingcap.tibuild.os":           "linux",
				"net.pingcap.tibuild.architecture": "amd64",
				"net.pingcap.tibuild.profile":      "release",
				"net.pingcap.tibuild.git-sha":      "1a0c3ac3292fff7742faa0c00a662ccb66ba40db",
				"net.pingcap.tibuild.tiup": []any{
					map[string]any{
						"description": "TiDB is an open source distributed HTAP database compatible with the MySQL protocol.",
						"entrypoint":  "tidb-server",
						"file":        "tidb-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]any{
						"description": "TiDB/TiKV cluster backup restore tool.",
						"entrypoint":  "br",
						"standalone":  true,
						"file":        "br-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]any{
						"description": "Dumpling is a CLI tool that helps you dump MySQL/TiDB data.",
						"entrypoint":  "dumpling",
						"file":        "dumpling-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]any{
						"description": "TiDB Lightning is a tool used for fast full import of large amounts of data into a TiDB cluster",
						"entrypoint":  "tidb-lightning",
						"standalone":  true,
						"file":        "tidb-lightning-v8.3.0-pre-linux-amd64.tar.gz",
					},
				},
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, _, err := FetchOCIArtifactConfig(tt.args.repo, tt.args.tag)
			if (err != nil) != tt.wantErr {
				t.Errorf("fetchOCIArtifactConfig() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("fetchOCIArtifactConfig() = %v, want %v", got, tt.want)
			}
		})
	}
}
