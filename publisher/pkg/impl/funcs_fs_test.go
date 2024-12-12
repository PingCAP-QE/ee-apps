package impl

import (
	"encoding/json"
	"reflect"
	"testing"
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
		want    []PublishRequest
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
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:b99b4e4f301bae87fa30fa58319da55bb6bdec94cbb29dccc35cf296815c3276",
							File: "tidb-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb",
						Version:    "v8.1.1#a7df4f9845d5d6a590c5d45dad0dcc9f21aa8765",
						EntryPoint: "linux_amd64/tidb-server.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:b99b4e4f301bae87fa30fa58319da55bb6bdec94cbb29dccc35cf296815c3276",
							File: "br-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "br",
						Version:    "v8.1.1#a7df4f9845d5d6a590c5d45dad0dcc9f21aa8765",
						EntryPoint: "linux_amd64/br.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:b99b4e4f301bae87fa30fa58319da55bb6bdec94cbb29dccc35cf296815c3276",
							File: "dumpling-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dumpling",
						Version:    "v8.1.1#a7df4f9845d5d6a590c5d45dad0dcc9f21aa8765",
						EntryPoint: "linux_amd64/dumpling.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:b99b4e4f301bae87fa30fa58319da55bb6bdec94cbb29dccc35cf296815c3276",
							File: "tidb-lightning-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb-lightning",
						Version:    "v8.1.1#a7df4f9845d5d6a590c5d45dad0dcc9f21aa8765",
						EntryPoint: "linux_amd64/tidb-lightning.tar.gz",
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
			want: []PublishRequest{
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:14e97372e2884406dc1b8c8a9390a1fbdd57d91eee67ba340032622409e5c288",
							File: "cdc-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "cdc",
						Version:    "v8.1.1#8ee0f3783a277161397d38bc62c48823de486b0d",
						EntryPoint: "linux_amd64/cdc.tar.gz",
					},
				},
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:14e97372e2884406dc1b8c8a9390a1fbdd57d91eee67ba340032622409e5c288",
							File: "dm-master-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dm-master",
						Version:    "v8.1.1#8ee0f3783a277161397d38bc62c48823de486b0d",
						EntryPoint: "linux_amd64/dm-master.tar.gz",
					},
				},
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:14e97372e2884406dc1b8c8a9390a1fbdd57d91eee67ba340032622409e5c288",
							File: "dm-worker-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dm-worker",
						Version:    "v8.1.1#8ee0f3783a277161397d38bc62c48823de486b0d",
						EntryPoint: "linux_amd64/dm-worker.tar.gz",
					},
				},
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:14e97372e2884406dc1b8c8a9390a1fbdd57d91eee67ba340032622409e5c288",
							File: "dmctl-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dmctl",
						Version:    "v8.1.1#8ee0f3783a277161397d38bc62c48823de486b0d",
						EntryPoint: "linux_amd64/dmctl.tar.gz",
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
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflash/package",
							Tag:  "sha256:4ba33a9106feb2189a5b1726155b6e1c15b102b8094956ece069afc01d9bb4a2",
							File: "tiflash-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tiflash",
						Version:    "v8.1.1#eb585f7d95d588bf8450c3cec02c36bb42c5e429",
						EntryPoint: "linux_amd64/tiflash.tar.gz",
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
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/tikv/pd/package",
							Tag:  "sha256:4d1222a01dd594176ec7f2dcf0b8e8cdaa2f838621d7738b56cdb39c9847f0a7",
							File: "pd-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "pd",
						Version:    "v8.1.1#f3dd0b62857ba0da97842349808aa4d4d4eefb34",
						EntryPoint: "linux_amd64/pd-server.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/tikv/pd/package",
							Tag:  "sha256:4d1222a01dd594176ec7f2dcf0b8e8cdaa2f838621d7738b56cdb39c9847f0a7",
							File: "pd-recover-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "pd-recover",
						Version:    "v8.1.1#f3dd0b62857ba0da97842349808aa4d4d4eefb34",
						EntryPoint: "linux_amd64/pd-recover.tar.gz",
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
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/tikv/tikv/package",
							Tag:  "sha256:b526c02e883d54f97162c445294e9aa805620d5af9979b24667958aff870be06",
							File: "tikv-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tikv",
						Version:    "v8.1.1#7793f1d5dc40206fe406ca001be1e0d7f1b83a8f",
						EntryPoint: "linux_amd64/tikv-server.tar.gz",
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
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb-tools/package",
							Tag:  "sha256:b50b45ffb0f53e3bf7c6140aa57fb768c4f2a9f6471e2987c423c0da217338b6",
							File: "tidb-tools-v8.1.1-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb-tools",
						Version:    "v8.1.1#d226440121147098eb5eb99cbc1efb94092ec68e",
						EntryPoint: "linux_amd64/tidb-tools.tar.gz",
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
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb-binlog/package",
							Tag:  "sha256:2c4704588ef754eaaed5d0034f44c6077257cebd382b90b2241b5e0ff4be640c",
							File: "binaries-v8.1.1-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb-binlog",
						Version:    "v8.1.1#79416e288d69bb530247546a846e7a89a8ef6d2f",
						EntryPoint: "linux_amd64/tidb-binlog.tar.gz",
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
		p    *PublishInfo
		want []string
	}{
		{
			name: "Basic version with commit hash",
			p: &PublishInfo{
				Name:       "tidb",
				Version:    "master#abc123def",
				EntryPoint: "bin/tidb-server",
			},
			want: []string{
				"download/builds/pingcap/tidb/master/abc123def/bin/tidb-server",
				"download/builds/pingcap/tidb/abc123def/bin/tidb-server",
			},
		},
		{
			name: "Version with multiple hash symbols",
			p: &PublishInfo{
				Name:       "pd",
				Version:    "release-5.0#abc",
				EntryPoint: "pd-server",
			},
			want: []string{
				"download/builds/pingcap/pd/release-5.0/abc/pd-server",
				"download/builds/pingcap/pd/abc/pd-server",
			},
		},
		{
			name: "Complex entry point path",
			p: &PublishInfo{
				Name:       "tiflash",
				Version:    "nightly#xyz789",
				EntryPoint: "opt/tiflash/bin/tiflash-server",
			},
			want: []string{
				"download/builds/pingcap/tiflash/nightly/xyz789/opt/tiflash/bin/tiflash-server",
				"download/builds/pingcap/tiflash/xyz789/opt/tiflash/bin/tiflash-server",
			},
		},
		{
			name: "tag",
			p: &PublishInfo{
				Name:       "tiflash",
				Version:    "v7.1.1#abcdef",
				EntryPoint: "opt/tiflash/bin/tiflash-server",
			},
			want: []string{
				"download/builds/pingcap/tiflash/v7.1.1/abcdef/opt/tiflash/bin/tiflash-server",
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
