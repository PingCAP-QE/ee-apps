package tiup

import (
	"reflect"
	"testing"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
)

func Test_transformTiupVer(t *testing.T) {
	type args struct {
		version string
		tag     string
	}
	tests := []struct {
		name string
		args args
		want string
	}{
		{
			name: "test1",
			args: args{
				version: "v8.5.0-pre",
				tag:     "v8.5.0-centos7_linux_amd64",
			},
			want: "v8.5.0-centos7",
		},
		{
			name: "test2",
			args: args{
				version: "v9.0.0-beta.1.pre",
				tag:     "v9.0.0-beta.1_linux_amd64",
			},
			want: "v9.0.0-beta.1",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := transformTiupVer(tt.args.version, tt.args.tag); got != tt.want {
				t.Errorf("transformTiupVer() = %v, want %v", got, tt.want)
			}
		})
	}
}

func Test_analyzeTiupFromOciArtifact(t *testing.T) {
	type args struct {
		repo string
		tag  string
	}
	tests := []struct {
		name    string
		args    args
		want    []PublishRequestTiUP
		wantErr bool
	}{
		{
			name: "TestFetchOCIArtifactConfig",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb/package",
				tag:  "v8.3.0_linux_amd64",
			},
			want: []PublishRequestTiUP{
				{
					From: share.From{
						Type: share.FromTypeOci,
						Oci: &share.FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "tidb-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfoTiUP{
						Version:     "v8.3.0",
						OS:          "linux",
						Arch:        "amd64",
						Name:        "tidb",
						Description: "TiDB is an open source distributed HTAP database compatible with the MySQL protocol.",
						EntryPoint:  "tidb-server",
					},
				},
				{
					From: share.From{
						Type: share.FromTypeOci,
						Oci: &share.FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "br-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfoTiUP{
						Version:     "v8.3.0",
						OS:          "linux",
						Arch:        "amd64",
						Name:        "br",
						Description: "TiDB/TiKV cluster backup restore tool.",
						EntryPoint:  "br",
					},
				},
				{
					From: share.From{
						Type: share.FromTypeOci,
						Oci: &share.FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "dumpling-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfoTiUP{
						Version:     "v8.3.0",
						OS:          "linux",
						Arch:        "amd64",
						Name:        "dumpling",
						Description: "Dumpling is a CLI tool that helps you dump MySQL/TiDB data.",
						EntryPoint:  "dumpling",
					},
				},
				{
					From: share.From{
						Type: share.FromTypeOci,
						Oci: &share.FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "tidb-lightning-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfoTiUP{
						Version:     "v8.3.0",
						OS:          "linux",
						Arch:        "amd64",
						Name:        "tidb-lightning",
						Description: "TiDB Lightning is a tool used for fast full import of large amounts of data into a TiDB cluster",
						EntryPoint:  "tidb-lightning",
					},
				},
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := analyzeTiupFromOciArtifact(tt.args.repo, tt.args.tag)
			if (err != nil) != tt.wantErr {
				t.Errorf("analyzeFromOciArtifact() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !reflect.DeepEqual(got[0], tt.want[0]) {
				t.Errorf("analyzeFromOciArtifact() = \n\t%v\n\twant = \n\t%v", got[0], tt.want[0])
			}
		})
	}
}

func Test_pkgName(t *testing.T) {
	tests := []struct {
		name        string
		tarballPath string
		want        string
	}{
		{
			name:        "Valid package name",
			tarballPath: "/path/to/tidb-v1.2.3-linux-amd64.tar.gz",
			want:        "tidb",
		},
		{
			name:        "Valid package name with numbers",
			tarballPath: "/path/to/tikv-v4.0.0-darwin-amd64.tar.gz",
			want:        "tikv",
		},
		{
			name:        "Invalid tarball name",
			tarballPath: "/path/to/invalid-file.txt",
			want:        "",
		},
		{
			name:        "Empty path",
			tarballPath: "",
			want:        "",
		},
		{
			name:        "Path with no filename",
			tarballPath: "/path/to/",
			want:        "",
		},
		{
			name:        "Package name with hyphens",
			tarballPath: "/downloads/tidb-lightning-v2.1.0-linux-amd64.tar.gz",
			want:        "tidb-lightning",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tiupPkgName(tt.tarballPath); got != tt.want {
				t.Errorf("pkgName() = %v, want %v", got, tt.want)
			}
		})
	}
}

func Test_transformVer(t *testing.T) {
	tests := []struct {
		name    string
		version string
		tag     string
		want    string
	}{
		{
			name:    "GA version",
			version: "v8.3.0-pre",
			tag:     "v8.3.0_linux_amd64",
			want:    "v8.3.0",
		},
		{
			name:    "Nightly version",
			version: "v8.3.0-alpha-12-gabcdef1",
			tag:     "master_linux_amd64",
			want:    "v8.3.0-alpha-nightly",
		},
		{
			name:    "RC version",
			version: "v8.3.0-pre",
			tag:     "release-8.3_linux_amd64",
			want:    "v8.3.0-pre",
		},
		{
			name:    "Nightly on main",
			version: "v8.3.0-alpha-12-gabcdef1",
			tag:     "main_linux_amd64",
			want:    "v8.3.0-alpha-nightly",
		},
		{
			name:    "hotfix version",
			version: "v8.3.0-20241201-abcdef",
			tag:     "v8.3.0-20241201-abcdef_linux_amd64",
			want:    "v8.3.0-20241201-abcdef",
		},
		{
			name:    "Empty version and tag",
			version: "",
			tag:     "",
			want:    "",
		},
		{
			name:    "GA version without -pre suffix",
			version: "v8.3.0",
			tag:     "v8.3.0_linux_amd64",
			want:    "v8.3.0",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := transformTiupVer(tt.version, tt.tag)
			if got != tt.want {
				t.Errorf("transformVer() = %v, want %v", got, tt.want)
			}
		})
	}
}
