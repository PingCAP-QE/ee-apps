package impl

import (
	"testing"
)

func TestExtractBuildStatus(t *testing.T) {
	srvc := &devbuildsrvc{}

	tests := []struct {
		name     string
		data     any
		expected string
	}{
		{
			name:     "nil data",
			data:     nil,
			expected: "",
		},
		{
			name:     "non-map data",
			data:     "invalid",
			expected: "",
		},
		{
			name:     "success status",
			data:     map[string]any{"status": "success"},
			expected: "success",
		},
		{
			name:     "succeeded status",
			data:     map[string]any{"status": "Succeeded"},
			expected: "success",
		},
		{
			name:     "completed status",
			data:     map[string]any{"status": "completed"},
			expected: "success",
		},
		{
			name:     "failure status",
			data:     map[string]any{"status": "failure"},
			expected: "failure",
		},
		{
			name:     "failed status",
			data:     map[string]any{"status": "Failed"},
			expected: "failure",
		},
		{
			name:     "error status",
			data:     map[string]any{"status": "error"},
			expected: "failure",
		},
		{
			name:     "running status",
			data:     map[string]any{"status": "running"},
			expected: "running",
		},
		{
			name:     "in_progress status",
			data:     map[string]any{"status": "in_progress"},
			expected: "running",
		},
		{
			name:     "unknown status",
			data:     map[string]any{"status": "pending"},
			expected: "pending",
		},
		{
			name:     "missing status field",
			data:     map[string]any{"other": "value"},
			expected: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := srvc.extractBuildStatus(tt.data)
			if result != tt.expected {
				t.Fatalf("expected %q, got %q", tt.expected, result)
			}
		})
	}
}

func TestExtractTektonStatus(t *testing.T) {
	srvc := &devbuildsrvc{}

	tests := []struct {
		name     string
		data     any
		expected bool
	}{
		{
			name:     "nil data",
			data:     nil,
			expected: false,
		},
		{
			name:     "non-map data",
			data:     "invalid",
			expected: false,
		},
		{
			name: "with pipeline info",
			data: map[string]any{
				"pipelineName": "test-pipeline",
				"status":       "Succeeded",
				"startTime":    "2024-01-01T00:00:00Z",
				"endTime":      "2024-01-01T01:00:00Z",
			},
			expected: true,
		},
		{
			name:     "without pipeline info",
			data:     map[string]any{"status": "success"},
			expected: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := srvc.extractTektonStatus(tt.data)
			if tt.expected && result == nil {
				t.Fatal("expected non-nil result, got nil")
			}
			if !tt.expected && result != nil {
				t.Fatalf("expected nil result, got %v", result)
			}
		})
	}
}
