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
			name:       "rejects empty worker name",
			workerName: "   ",
			workerArch: "amd64",
			expectErr:  true,
		},
		{
			name:       "rejects unsupported worker arch",
			workerName: "mac-mini-01",
			workerArch: "ppc64le",
			expectErr:  true,
		},
	}

	for _, test := range tests {
		test := test
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
