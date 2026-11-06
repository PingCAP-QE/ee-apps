package impl

import "testing"

func TestIsNewTidbxGitTag(t *testing.T) {
	tests := []struct {
		tag  string
		want bool
	}{
		{tag: "v25.12.9", want: false},
		{tag: "v26.0.0", want: true},
		{tag: "v26.3.1", want: true},
		{tag: "v26.3.1-nextgen", want: false},
		{tag: "v8.5.4-nextgen.202510.1", want: false},
	}

	for _, tt := range tests {
		if got := isNewTidbxGitTag(tt.tag); got != tt.want {
			t.Fatalf("isNewTidbxGitTag(%q) = %v, want %v", tt.tag, got, tt.want)
		}
	}
}

func TestNormalizeTidbxQueryTag(t *testing.T) {
	tests := []struct {
		tag  string
		want string
	}{
		{tag: "v25.12.9-nextgen", want: "v25.12.9-nextgen"},
		{tag: "v26.0.0-nextgen", want: "v26.0.0"},
		{tag: "v26.3.1-nextgen", want: "v26.3.1"},
		{tag: "v8.5.4-nextgen.202510.1", want: "v8.5.4-nextgen.202510.1"},
	}

	for _, tt := range tests {
		if got := normalizeTidbxQueryTag(tt.tag); got != tt.want {
			t.Fatalf("normalizeTidbxQueryTag(%q) = %q, want %q", tt.tag, got, tt.want)
		}
	}
}
