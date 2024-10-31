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
				tag:  "master_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:1b146aa1b65e0a4fb2044f464a7fbceb025fcd6a2ddf0c28ab53f671503eea9d",
							File: "tidb-v8.5.0-alpha-22-ga22fc590cc-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb",
						Version:    "master#a22fc590cc9efb13c025386712f39338c9821187",
						EntryPoint: "centos7/tidb-server.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:1b146aa1b65e0a4fb2044f464a7fbceb025fcd6a2ddf0c28ab53f671503eea9d",
							File: "br-v8.5.0-alpha-22-ga22fc590cc-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "br",
						Version:    "master#a22fc590cc9efb13c025386712f39338c9821187",
						EntryPoint: "centos7/br.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:1b146aa1b65e0a4fb2044f464a7fbceb025fcd6a2ddf0c28ab53f671503eea9d",
							File: "dumpling-v8.5.0-alpha-22-ga22fc590cc-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dumpling",
						Version:    "master#a22fc590cc9efb13c025386712f39338c9821187",
						EntryPoint: "centos7/dumpling.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "sha256:1b146aa1b65e0a4fb2044f464a7fbceb025fcd6a2ddf0c28ab53f671503eea9d",
							File: "tidb-lightning-v8.5.0-alpha-22-ga22fc590cc-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb-lightning",
						Version:    "master#a22fc590cc9efb13c025386712f39338c9821187",
						EntryPoint: "centos7/tidb-lightning.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tiflow",
			args: args{
				repo: "hub.pingcap.net/pingcap/tiflow/package",
				tag:  "master_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:3bd51f5057646e3d7894573d07af1af63d94336f9323c67106caf265c191054f",
							File: "cdc-v8.5.0-alpha-3-g0510cf054-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "cdc",
						Version:    "master#0510cf05400ec2302052a517d281bf3aff7cfc04",
						EntryPoint: "centos7/cdc.tar.gz",
					},
				},
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:3bd51f5057646e3d7894573d07af1af63d94336f9323c67106caf265c191054f",
							File: "dm-master-v8.5.0-alpha-3-g0510cf054-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dm-master",
						Version:    "master#0510cf05400ec2302052a517d281bf3aff7cfc04",
						EntryPoint: "centos7/dm-master.tar.gz",
					},
				},
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:3bd51f5057646e3d7894573d07af1af63d94336f9323c67106caf265c191054f",
							File: "dm-worker-v8.5.0-alpha-3-g0510cf054-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dm-worker",
						Version:    "master#0510cf05400ec2302052a517d281bf3aff7cfc04",
						EntryPoint: "centos7/dm-worker.tar.gz",
					},
				},
				{
					From: From{
						Type: "oci",
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflow/package",
							Tag:  "sha256:3bd51f5057646e3d7894573d07af1af63d94336f9323c67106caf265c191054f",
							File: "dmctl-v8.5.0-alpha-3-g0510cf054-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "dmctl",
						Version:    "master#0510cf05400ec2302052a517d281bf3aff7cfc04",
						EntryPoint: "centos7/dmctl.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tiflash",
			args: args{
				repo: "hub.pingcap.net/pingcap/tiflash/package",
				tag:  "master_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tiflash/package",
							Tag:  "sha256:0903955bbcf4ce8306af7f3138863ed899c5000112cd64d04ce07bb7dd9a816a",
							File: "tiflash-v8.5.0-alpha-7-g5dd3a733a-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tiflash",
						Version:    "master#5dd3a733a2dec9ee3548353356583b21cd03d54d",
						EntryPoint: "centos7/tiflash.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - pd",
			args: args{
				repo: "hub.pingcap.net/tikv/pd/package",
				tag:  "master_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/tikv/pd/package",
							Tag:  "sha256:eac8e279cd5acb2fa900640175418837c3f8517299d76a7509afd5ab9c6f939d",
							File: "pd-v8.5.0-alpha-2-g649393a4-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "pd",
						Version:    "master#649393a40725e9c91397790ede93dd3a0b7f08ef",
						EntryPoint: "centos7/pd.tar.gz",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/tikv/pd/package",
							Tag:  "sha256:eac8e279cd5acb2fa900640175418837c3f8517299d76a7509afd5ab9c6f939d",
							File: "pd-recover-v8.5.0-alpha-2-g649393a4-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "pd-recover",
						Version:    "master#649393a40725e9c91397790ede93dd3a0b7f08ef",
						EntryPoint: "centos7/pd-recover.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tikv",
			args: args{
				repo: "hub.pingcap.net/tikv/tikv/package",
				tag:  "master_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/tikv/tikv/package",
							Tag:  "sha256:c7a9b221bed19c885bf8a1bc00af1fb35cc5d789337c842508682eea4064f2fa",
							File: "tikv-v8.5.0-alpha-2-gdc9cd3dbd-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tikv",
						Version:    "master#dc9cd3dbdace23ac1f590aaee7966705a7e12825",
						EntryPoint: "centos7/tikv.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tidb-tools",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb-tools/package",
				tag:  "master_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb-tools/package",
							Tag:  "sha256:315dbe7cc91fb998f9b2f3cd256f11d236cf235537a0b8f27457381c6cd30b19",
							File: "tidb-tools-v7.5.3-2-gd226440-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb-tools",
						Version:    "master#d226440121147098eb5eb99cbc1efb94092ec68e",
						EntryPoint: "centos7/tidb-tools.tar.gz",
					},
				},
			},
			wantErr: false,
		},
		{
			name: "Valid fs config - tidb-binlog",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb-binlog/package",
				tag:  "master_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb-binlog/package",
							Tag:  "sha256:437cf5943318cebae48adc6698380537a2c5173422ba7048196124c198aaa1b8",
							File: "binaries-v8.3.0-alpha-1-g6905951-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Name:       "tidb-binlog",
						Version:    "master#6905951ca9460e2d4e5a82273e01f6a36b4d1ef3",
						EntryPoint: "centos7/tidb-binlog.tar.gz",
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
				"pingcap/tidb/master/abc123def/bin/tidb-server",
				"pingcap/tidb/abc123def/bin/tidb-server",
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
				"pingcap/pd/release-5.0/abc/pd-server",
				"pingcap/pd/abc/pd-server",
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
				"pingcap/tiflash/nightly/xyz789/opt/tiflash/bin/tiflash-server",
				"pingcap/tiflash/xyz789/opt/tiflash/bin/tiflash-server",
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
