package impl

import "testing"

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
