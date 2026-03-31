package tidbcloud

import "testing"

func TestIsSupportedNextgenImageTag(t *testing.T) {
	tests := []struct {
		tag  string
		want bool
	}{
		{tag: "v8.5.4-nextgen.202510.1", want: true},
		{tag: "v26.3.0-nextgen", want: true},
		{tag: "v26.3.1-nextgen", want: true},
		{tag: "v26.3.1-2-gabcdef0-nextgen", want: false},
		{tag: "v26.3.1", want: false},
		{tag: "master-next-gen", want: false},
	}

	for _, tt := range tests {
		if got := isSupportedNextgenImageTag(tt.tag); got != tt.want {
			t.Fatalf("isSupportedNextgenImageTag(%q) = %v, want %v", tt.tag, got, tt.want)
		}
	}
}
