package tidbcloud

import "testing"

func TestIsSupportedNextgenImageTag(t *testing.T) {
	tests := []struct {
		tag  string
		want bool
	}{
		{tag: "v8.5.4-nextgen.202510.1", want: true},
		{tag: "v25.12.9-nextgen", want: false},
		{tag: "v26.3.0-nextgen", want: true},
		{tag: "v26.0.0-nextgen", want: true},
		{tag: "v26.3.1-nextgen", want: true},
		{tag: "v26.3.1", want: true},
		{tag: "v26.3.1-2-gabcdef0-nextgen", want: false},
		{tag: "master-next-gen", want: false},
	}

	for _, tt := range tests {
		if got := isSupportedNextgenImageTag(tt.tag); got != tt.want {
			t.Fatalf("isSupportedNextgenImageTag(%q) = %v, want %v", tt.tag, got, tt.want)
		}
	}
}

func TestNormalizeComponentVersion(t *testing.T) {
	tests := []struct {
		version string
		want    string
	}{
		{version: "v26.3.0", want: "v26.3.99"},
		{version: "v26.3.1", want: "v26.3.99"},
		{version: "v26.3.2", want: "v26.3.99"},
		{version: "v26.3.99", want: "v26.3.99"},
		{version: "v26.3.100", want: "v26.3.99"},
		{version: "v26.2.0", want: "v26.2.0"},
		{version: "v26.4.0", want: "v26.4.0"},
		{version: "v27.0.0", want: "v27.0.0"},
		{version: "v8.5.4", want: "v8.5.4"},
		{version: "v25.12.9", want: "v25.12.9"},
	}

	for _, tt := range tests {
		if got := normalizeComponentVersion(tt.version); got != tt.want {
			t.Fatalf("normalizeComponentVersion(%q) = %q, want %q", tt.version, got, tt.want)
		}
	}
}
