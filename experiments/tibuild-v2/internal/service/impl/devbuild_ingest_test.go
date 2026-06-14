package impl

import (
	"testing"
)

func TestExtractBuildStatusFromEventType(t *testing.T) {
	srvc := &devbuildsrvc{}

	tests := []struct {
		name      string
		eventType string
		expected  string
	}{
		{
			name:      "started event",
			eventType: "dev.tekton.event.pipelinerun.started.v1",
			expected:  "running",
		},
		{
			name:      "successful event",
			eventType: "dev.tekton.event.pipelinerun.successful.v1",
			expected:  "success",
		},
		{
			name:      "failed event",
			eventType: "dev.tekton.event.pipelinerun.failed.v1",
			expected:  "failure",
		},
		{
			name:      "unknown event type",
			eventType: "unknown.event.type",
			expected:  "",
		},
		{
			name:      "empty event type",
			eventType: "",
			expected:  "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := srvc.extractBuildStatusFromEventType(tt.eventType)
			if result != tt.expected {
				t.Fatalf("expected %q, got %q", tt.expected, result)
			}
		})
	}
}

func TestExtractDevBuildID_FromAnnotations(t *testing.T) {
	srvc := &devbuildsrvc{}

	tests := []struct {
		name      string
		data      any
		source    string
		expected  int
		expectErr bool
	}{
		{
			name: "valid ce-context annotation",
			data: map[string]any{
				"pipelineRun": map[string]any{
					"metadata": map[string]any{
						"annotations": map[string]any{
							"tekton.dev/ce-context": `{"source":"tibuild.pingcap.net/api/devbuilds/123","subject":"123"}`,
						},
					},
				},
			},
			source:   "tekton",
			expected: 123,
		},
		{
			name: "non-devbuild source in ce-context",
			data: map[string]any{
				"pipelineRun": map[string]any{
					"metadata": map[string]any{
						"annotations": map[string]any{
							"tekton.dev/ce-context": `{"source":"other.source","subject":"456"}`,
						},
					},
				},
			},
			source:   "tekton",
			expected: 0,
		},
		{
			name: "fallback to event source",
			data:   nil,
			source: "tibuild.pingcap.net/api/devbuilds/789",
			expected: 789,
		},
		{
			name:      "nil data and non-devbuild source",
			data:      nil,
			source:    "other.source",
			expected:  0,
		},
		{
			name: "invalid ce-context json",
			data: map[string]any{
				"pipelineRun": map[string]any{
					"metadata": map[string]any{
						"annotations": map[string]any{
							"tekton.dev/ce-context": "invalid json",
						},
					},
				},
			},
			source:   "tekton",
			expected: 0,
		},
		{
			name: "missing annotations",
			data: map[string]any{
				"pipelineRun": map[string]any{
					"metadata": map[string]any{},
				},
			},
			source:   "tekton",
			expected: 0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result, err := srvc.extractDevBuildID(tt.data, tt.source)
			if tt.expectErr && err == nil {
				t.Fatal("expected error, got nil")
			}
			if !tt.expectErr && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if result != tt.expected {
				t.Fatalf("expected %d, got %d", tt.expected, result)
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
