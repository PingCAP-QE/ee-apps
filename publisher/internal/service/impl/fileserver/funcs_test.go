package fileserver

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
)

func Test_analyzeFsFromOciArtifact(t *testing.T) {
	// t.Skipf("maybe it is out of date")
	type args struct {
		repo string
		tag  string
	}
	tests := []struct {
		name    string
		args    args
		want    *PublishRequestFS
		wantErr bool
	}{
		{
			name: "Empty config",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb/package",
				tag:  "empty_config",
			},
			want:    nil,
			wantErr: true,
		},
		{
			name: "Valid fs config - tidb",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb/package",
				tag:  "v8.1.1_linux_amd64",
			},
			want: &PublishRequestFS{
				From: share.From{
					Type: share.FromTypeOci,
					Oci: &share.FromOci{
						Repo: "hub.pingcap.net/pingcap/tidb/package",
						Tag:  "sha256:b99b4e4f301bae87fa30fa58319da55bb6bdec94cbb29dccc35cf296815c3276",
					},
				},
				// target key: download/builds/<full-repo>/<branch>/<commit_sha>/<map-val-tarball>
				// tikv: download/builds/tikv/tkv/master/<commit_sha>/tikv.tar.gz
				// tidb: download/builds/pingcap/tidb/master/<commit_sha>/tidb.tar.gz
				Publish: PublishInfoFS{
					Repo:      "pingcap/tidb",
					Branch:    "v8.1.1",
					CommitSHA: "a7df4f9845d5d6a590c5d45dad0dcc9f21aa8765",
					FileTransferMap: map[string]string{
						"tidb-v8.1.1-pre-linux-amd64.tar.gz":               "linux_amd64/tidb.tar.gz",
						"br-v8.1.1-pre-linux-amd64.tar.gz":                 "linux_amd64/br.tar.gz",
						"dumpling-v8.1.1-pre-linux-amd64.tar.gz":           "linux_amd64/dumpling.tar.gz",
						"tidb-lightning-v8.1.1-pre-linux-amd64.tar.gz":     "linux_amd64/tidb-lightning.tar.gz",
						"tidb-lightning-ctl-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/tidb-lightning-ctl.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tiflow",
			args: args{
				repo: "hub.pingcap.net/pingcap/tiflow/package",
				tag:  "v8.1.1_linux_amd64",
			},
			want: &PublishRequestFS{
				From: share.From{
					Type: share.FromTypeOci,
					Oci: &share.FromOci{
						Repo: "hub.pingcap.net/pingcap/tiflow/package",
						Tag:  "sha256:14e97372e2884406dc1b8c8a9390a1fbdd57d91eee67ba340032622409e5c288",
					},
				},
				Publish: PublishInfoFS{
					Repo:      "pingcap/tiflow",
					Branch:    "v8.1.1",
					CommitSHA: "8ee0f3783a277161397d38bc62c48823de486b0d",
					FileTransferMap: map[string]string{
						"cdc-v8.1.1-pre-linux-amd64.tar.gz":       "linux_amd64/cdc.tar.gz",
						"dm-master-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/dm-master.tar.gz",
						"dm-worker-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/dm-worker.tar.gz",
						"dmctl-v8.1.1-pre-linux-amd64.tar.gz":     "linux_amd64/dmctl.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tiflash",
			args: args{
				repo: "hub.pingcap.net/pingcap/tiflash/package",
				tag:  "v8.1.1_linux_amd64",
			},
			want: &PublishRequestFS{
				From: share.From{
					Type: share.FromTypeOci,
					Oci: &share.FromOci{
						Repo: "hub.pingcap.net/pingcap/tiflash/package",
						Tag:  "sha256:4ba33a9106feb2189a5b1726155b6e1c15b102b8094956ece069afc01d9bb4a2",
					},
				},
				Publish: PublishInfoFS{
					Repo:      "pingcap/tiflash",
					Branch:    "v8.1.1",
					CommitSHA: "eb585f7d95d588bf8450c3cec02c36bb42c5e429",
					FileTransferMap: map[string]string{
						"tiflash-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/tiflash.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - pd",
			args: args{
				repo: "hub.pingcap.net/tikv/pd/package",
				tag:  "v8.1.1_linux_amd64",
			},
			want: &PublishRequestFS{
				From: share.From{
					Type: share.FromTypeOci,
					Oci: &share.FromOci{
						Repo: "hub.pingcap.net/tikv/pd/package",
						Tag:  "sha256:4d1222a01dd594176ec7f2dcf0b8e8cdaa2f838621d7738b56cdb39c9847f0a7",
					},
				},
				Publish: PublishInfoFS{
					Repo:      "tikv/pd",
					Branch:    "v8.1.1",
					CommitSHA: "f3dd0b62857ba0da97842349808aa4d4d4eefb34",
					FileTransferMap: map[string]string{
						"pd-v8.1.1-pre-linux-amd64.tar.gz":         "linux_amd64/pd.tar.gz",
						"pd-recover-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/pd-recover.tar.gz",
						"pd-ctl-v8.1.1-pre-linux-amd64.tar.gz":     "linux_amd64/pd-ctl.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tikv",
			args: args{
				repo: "hub.pingcap.net/tikv/tikv/package",
				tag:  "v8.1.1_linux_amd64",
			},
			want: &PublishRequestFS{
				From: share.From{
					Type: share.FromTypeOci,
					Oci: &share.FromOci{
						Repo: "hub.pingcap.net/tikv/tikv/package",
						Tag:  "sha256:b526c02e883d54f97162c445294e9aa805620d5af9979b24667958aff870be06",
					},
				},
				Publish: PublishInfoFS{
					Repo:      "tikv/tikv",
					Branch:    "v8.1.1",
					CommitSHA: "7793f1d5dc40206fe406ca001be1e0d7f1b83a8f",
					FileTransferMap: map[string]string{
						"tikv-v8.1.1-pre-linux-amd64.tar.gz":     "linux_amd64/tikv.tar.gz",
						"tikv-ctl-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/tikv-ctl.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tidb-tools",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb-tools/package",
				tag:  "v8.1.1_linux_amd64",
			},
			want: &PublishRequestFS{
				From: share.From{
					Type: share.FromTypeOci,
					Oci: &share.FromOci{
						Repo: "hub.pingcap.net/pingcap/tidb-tools/package",
						Tag:  "sha256:b50b45ffb0f53e3bf7c6140aa57fb768c4f2a9f6471e2987c423c0da217338b6",
					},
				},
				Publish: PublishInfoFS{
					Repo:      "pingcap/tidb-tools",
					Branch:    "v8.1.1",
					CommitSHA: "d226440121147098eb5eb99cbc1efb94092ec68e",
					FileTransferMap: map[string]string{
						"tidb-tools-v8.1.1-linux-amd64.tar.gz": "linux_amd64/tidb-tools.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tidb-binlog",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb-binlog/package",
				tag:  "v8.1.1_linux_amd64",
			},
			want: &PublishRequestFS{
				From: share.From{
					Type: share.FromTypeOci,
					Oci: &share.FromOci{
						Repo: "hub.pingcap.net/pingcap/tidb-binlog/package",
						Tag:  "sha256:2c4704588ef754eaaed5d0034f44c6077257cebd382b90b2241b5e0ff4be640c",
					},
				},
				Publish: PublishInfoFS{
					Repo:      "pingcap/tidb-binlog",
					Branch:    "v8.1.1",
					CommitSHA: "79416e288d69bb530247546a846e7a89a8ef6d2f",
					FileTransferMap: map[string]string{
						"binaries-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/binaries.tar.gz",
						"drainer-v8.1.1-pre-linux-amd64.tar.gz":  "linux_amd64/drainer.tar.gz",
						"pump-v8.1.1-pre-linux-amd64.tar.gz":     "linux_amd64/pump.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Invalid repo",
			args: args{
				repo: "invalid-repo",
				tag:  "v1.0.0",
			},
			want:    nil,
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := analyzeFsFromOciArtifact(tt.args.repo, tt.args.tag)
			if (err != nil) != tt.wantErr {
				t.Errorf("analyzeFsFromOciArtifact() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !reflect.DeepEqual(got, tt.want) {
				gotBytes, _ := json.MarshalIndent(got, "", "  ")
				wantBytes, _ := json.MarshalIndent(tt.want, "", "  ")
				t.Errorf("analyzeFsFromOciArtifact() = \n%s, want \n%s", gotBytes, wantBytes)
			}
		})
	}
}
func Test_targetFsFullPaths(t *testing.T) {
	tests := []struct {
		name string
		p    *PublishInfoFS
		want map[string]string
	}{
		{
			name: "Basic version with commit hash - tidb",
			p: &PublishInfoFS{
				Repo:      "pingcap/tidb",
				Branch:    "master",
				CommitSHA: "abc123def",
				FileTransferMap: map[string]string{
					"tidb-v8.1.1-pre-linux-amd64.tar.gz":               "linux_amd64/tidb.tar.gz",
					"br-v8.1.1-pre-linux-amd64.tar.gz":                 "linux_amd64/br.tar.gz",
					"dumpling-v8.1.1-pre-linux-amd64.tar.gz":           "linux_amd64/dumpling.tar.gz",
					"tidb-lightning-v8.1.1-pre-linux-amd64.tar.gz":     "linux_amd64/tidb-lightning.tar.gz",
					"tidb-lightning-ctl-v8.1.1-pre-linux-amd64.tar.gz": "linux_amd64/tidb-lightning-ctl.tar.gz",
				},
			},
			want: map[string]string{
				"tidb-v8.1.1-pre-linux-amd64.tar.gz":               "download/builds/pingcap/tidb/master/abc123def/linux_amd64/tidb.tar.gz",
				"br-v8.1.1-pre-linux-amd64.tar.gz":                 "download/builds/pingcap/tidb/master/abc123def/linux_amd64/br.tar.gz",
				"dumpling-v8.1.1-pre-linux-amd64.tar.gz":           "download/builds/pingcap/tidb/master/abc123def/linux_amd64/dumpling.tar.gz",
				"tidb-lightning-v8.1.1-pre-linux-amd64.tar.gz":     "download/builds/pingcap/tidb/master/abc123def/linux_amd64/tidb-lightning.tar.gz",
				"tidb-lightning-ctl-v8.1.1-pre-linux-amd64.tar.gz": "download/builds/pingcap/tidb/master/abc123def/linux_amd64/tidb-lightning-ctl.tar.gz",
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := targetFsFullPaths(tt.p)
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("targetFsFullPaths() = %v, want %v", got, tt.want)
			}
		})
	}
}
func Test_getFileKeys(t *testing.T) {
	tests := []struct {
		name    string
		files   []string
		want    map[string]string
		wantErr bool
	}{
		{
			name:  "Empty file list",
			files: []string{},
			want:  map[string]string{},
		},
		{
			name: "Multiple valid tarballs",
			files: []string{
				"tidb-server-v1.1.1-linux-amd64.tar.gz",
				"tikv-server-v2.0.0-darwin-arm64.tar.gz",
				"tidb-server-v1.1.1-alpha-pre-linux-amd64.tar.gz",
			},
			want: map[string]string{
				"tidb-server-v1.1.1-linux-amd64.tar.gz":           "linux_amd64/tidb-server.tar.gz",
				"tikv-server-v2.0.0-darwin-arm64.tar.gz":          "darwin_arm64/tikv-server.tar.gz",
				"tidb-server-v1.1.1-alpha-pre-linux-amd64.tar.gz": "linux_amd64/tidb-server.tar.gz",
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := getFileKeys(tt.files)
			if (err != nil) != tt.wantErr {
				t.Errorf("newFunction() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("newFunction() = %v, want %v", got, tt.want)
			}
		})
	}
}
