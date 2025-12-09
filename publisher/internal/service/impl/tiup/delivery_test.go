package tiup

import (
	"testing"
)

// TestComputeDeliveryInstructionsForRule covers various combinations of DeliveryRule settings.
func TestComputeDeliveryInstructionsForRule(t *testing.T) {
	strPtr := func(v string) *string { return &v }
	tests := []struct {
		name               string
		rule               DeliveryRule
		ociTag             string
		wantMirrors        []string
		wantVersionValues  []string // if versionShouldBeNil[i] is true, this entry is ignored
		versionShouldBeNil []bool
	}{
		{
			name: "nextgen EA/GA rule expects $1 capture of tag",
			rule: DeliveryRule{
				DestMirrors:     []string{"staging"},
				TagsRegex:       []string{"^(v[0-9]+[.][0-9]+[.][0-9]+-nextgen[.][0-9]+[.][0-9]+)_(linux|darwin)_(amd64|arm64)$"},
				TagRegexReplace: strPtr("$1"),
			},
			ociTag:             "v8.5.4-nextgen.202511.0_linux_amd64",
			wantMirrors:        []string{"staging"},
			wantVersionValues:  []string{"v8.5.4-nextgen.202511.0"},
			versionShouldBeNil: []bool{false},
		},
		{
			name: "versioin replace matched first pattern",
			rule: DeliveryRule{
				DestMirrors:     []string{"staging"},
				TagsRegex:       []string{"^(feature-abc)_(linux|darwin)_(amd64|arm64)$", "^(debug-def)_(linux|darwin)_(amd64|arm64)$"},
				TagRegexReplace: strPtr("$1-feature"),
			},
			ociTag:             "feature-abc_linux_amd64",
			wantMirrors:        []string{"staging"},
			wantVersionValues:  []string{"feature-abc-feature"},
			versionShouldBeNil: []bool{false},
		},
		{
			name: "versioin replace matched last pattern",
			rule: DeliveryRule{
				DestMirrors:     []string{"staging"},
				TagsRegex:       []string{"^(feature-abc)_(linux|darwin)_(amd64|arm64)$", "^(debug-def)_(linux|darwin)_(amd64|arm64)$"},
				TagRegexReplace: strPtr("$1-feature"),
			},
			ociTag:             "debug-def_linux_amd64",
			wantMirrors:        []string{"staging"},
			wantVersionValues:  []string{"debug-def-feature"},
			versionShouldBeNil: []bool{false},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := computeDeliveryInstructionsForRule(tt.rule, "test.com/repo", tt.ociTag)
			if len(got) != len(tt.wantMirrors) {
				t.Fatalf("len(got)=%d, want %d", len(got), len(tt.wantMirrors))
			}
			for i, inst := range got {
				if inst.TiupMirror != tt.wantMirrors[i] {
					t.Errorf("instruction[%d].TiupMirror=%q, want %q", i, inst.TiupMirror, tt.wantMirrors[i])
				}
				if tt.versionShouldBeNil[i] {
					if inst.Version != nil {
						t.Errorf("instruction[%d].Version expected nil, got %q", i, *inst.Version)
					}
				} else {
					if inst.Version == nil {
						t.Errorf("instruction[%d].Version expected non-nil, got nil", i)
					} else if *inst.Version != tt.wantVersionValues[i] {
						t.Errorf("instruction[%d].Version=%q, want %q", i, *inst.Version, tt.wantVersionValues[i])
					}
				}
			}
		})
	}
}
