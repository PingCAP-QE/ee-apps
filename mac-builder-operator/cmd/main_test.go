package main

import "testing"

func TestValidateAgentIdentity(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name         string
		workerName   string
		workerArch   string
		expectedName string
		expectedArch string
		expectErr    bool
	}{
		{
			name:         "normalizes whitespace and arch casing",
			workerName:   "  mac-mini-01  ",
			workerArch:   " ARM64 ",
			expectedName: "mac-mini-01",
			expectedArch: "arm64",
		},
		{
			name:         "allows an empty name to use the derived machine identity",
			workerName:   "   ",
			workerArch:   "amd64",
			expectedName: "",
			expectedArch: "amd64",
		},
		{
			name:       "rejects unsupported worker arch",
			workerName: "mac-mini-01",
			workerArch: "ppc64le",
			expectErr:  true,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			gotName, gotArch, err := validateAgentIdentity(test.workerName, test.workerArch)
			if test.expectErr {
				if err == nil {
					t.Fatalf("expected validation error, got success (%q, %q)", gotName, gotArch)
				}
				return
			}
			if err != nil {
				t.Fatalf("validateAgentIdentity returned error: %v", err)
			}
			if gotName != test.expectedName {
				t.Fatalf("expected worker name %q, got %q", test.expectedName, gotName)
			}
			if gotArch != test.expectedArch {
				t.Fatalf("expected worker arch %q, got %q", test.expectedArch, gotArch)
			}
		})
	}
}

func TestParseLabels(t *testing.T) {
	t.Parallel()

	labels, err := parseLabels(" tibuild.pingcap.net/pool=mac-mini , arch=arm64, empty= ")
	if err != nil {
		t.Fatalf("parse labels: %v", err)
	}
	if labels["tibuild.pingcap.net/pool"] != "mac-mini" || labels["arch"] != "arm64" {
		t.Fatalf("unexpected labels: %#v", labels)
	}
	if value, ok := labels["empty"]; !ok || value != "" {
		t.Fatalf("expected empty label value, got %#v", labels)
	}

	if _, err := parseLabels("missing-equals"); err == nil {
		t.Fatal("expected an error for a label without '='")
	}
	if _, err := parseLabels("=value"); err == nil {
		t.Fatal("expected an error for an empty label key")
	}

	empty, err := parseLabels(" , ")
	if err != nil || len(empty) != 0 {
		t.Fatalf("expected no labels, got %#v (err=%v)", empty, err)
	}
}
