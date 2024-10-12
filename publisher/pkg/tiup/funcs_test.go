package tiup

import (
	"reflect"
	"testing"
)

func Test_fetchOCIArtifactConfig(t *testing.T) {
	type args struct {
		repo string
		tag  string
	}
	tests := []struct {
		name    string
		args    args
		want    map[string]interface{}
		wantErr bool
	}{
		{
			name: "TestFetchOCIArtifactConfig",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb/package",
				tag:  "v8.3.0_linux_amd64",
			},
			want: map[string]interface{}{
				"org.opencontainers.image.version": "v8.3.0-pre",
				"net.pingcap.tibuild.os":           "linux",
				"net.pingcap.tibuild.architecture": "amd64",
				"net.pingcap.tibuild.profile":      "release",
				"net.pingcap.tibuild.git-sha":      "1a0c3ac3292fff7742faa0c00a662ccb66ba40db",
				"net.pingcap.tibuild.tiup": []interface{}{
					map[string]interface{}{
						"description": "TiDB is an open source distributed HTAP database compatible with the MySQL protocol.",
						"entrypoint":  "tidb-server",
						"file":        "tidb-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]interface{}{
						"description": "TiDB/TiKV cluster backup restore tool.",
						"entrypoint":  "br",
						"standalone":  true,
						"file":        "br-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]interface{}{
						"description": "Dumpling is a CLI tool that helps you dump MySQL/TiDB data.",
						"entrypoint":  "dumpling",
						"file":        "dumpling-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]interface{}{
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
			want: map[string]interface{}{
				"org.opencontainers.image.version": "v8.3.0-pre",
				"net.pingcap.tibuild.os":           "linux",
				"net.pingcap.tibuild.architecture": "amd64",
				"net.pingcap.tibuild.profile":      "release",
				"net.pingcap.tibuild.git-sha":      "1a0c3ac3292fff7742faa0c00a662ccb66ba40db",
				"net.pingcap.tibuild.tiup": []interface{}{
					map[string]interface{}{
						"description": "TiDB is an open source distributed HTAP database compatible with the MySQL protocol.",
						"entrypoint":  "tidb-server",
						"file":        "tidb-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]interface{}{
						"description": "TiDB/TiKV cluster backup restore tool.",
						"entrypoint":  "br",
						"standalone":  true,
						"file":        "br-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]interface{}{
						"description": "Dumpling is a CLI tool that helps you dump MySQL/TiDB data.",
						"entrypoint":  "dumpling",
						"file":        "dumpling-v8.3.0-pre-linux-amd64.tar.gz",
					},
					map[string]interface{}{
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
			got, _, err := fetchOCIArtifactConfig(tt.args.repo, tt.args.tag)
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

func Test_analyzeFromOciArtifact(t *testing.T) {
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
			name: "TestFetchOCIArtifactConfig",
			args: args{
				repo: "hub.pingcap.net/pingcap/tidb/package",
				tag:  "v8.3.0_linux_amd64",
			},
			want: []PublishRequest{
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "tidb-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Version:     "v8.3.0",
						OS:          "linux",
						Arch:        "amd64",
						Name:        "tidb",
						Description: "TiDB is an open source distributed HTAP database compatible with the MySQL protocol.",
						EntryPoint:  "tidb-server",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "br-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Version:     "v8.3.0",
						OS:          "linux",
						Arch:        "amd64",
						Name:        "br",
						Description: "TiDB/TiKV cluster backup restore tool.",
						EntryPoint:  "br",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "dumpling-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
						Version:     "v8.3.0",
						OS:          "linux",
						Arch:        "amd64",
						Name:        "dumpling",
						Description: "Dumpling is a CLI tool that helps you dump MySQL/TiDB data.",
						EntryPoint:  "dumpling",
					},
				},
				{
					From: From{
						Type: FromTypeOci,
						Oci: &FromOci{
							Repo: "hub.pingcap.net/pingcap/tidb/package",
							Tag:  "v8.3.0_linux_amd64",
							File: "tidb-lightning-v8.3.0-pre-linux-amd64.tar.gz",
						},
					},
					Publish: PublishInfo{
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
			got, err := AnalyzeFromOciArtifact(tt.args.repo, tt.args.tag)
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
			if got := pkgName(tt.tarballPath); got != tt.want {
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
			got := transformVer(tt.version, tt.tag)
			if got != tt.want {
				t.Errorf("transformVer() = %v, want %v", got, tt.want)
			}
		})
	}
}
