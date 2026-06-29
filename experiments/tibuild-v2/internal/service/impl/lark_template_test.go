package impl

import (
	"encoding/json"
	"testing"
)

func TestNewLarkCardJSON(t *testing.T) {
	tests := []struct {
		name    string
		info    *NotificationInfo
		wantErr bool
	}{
		{
			name: "success status",
			info: &NotificationInfo{
				BuildID:  1001,
				Product:  "tidb",
				Version:  "v8.5.0",
				Status:   "success",
				Platform: "linux/amd64",
				GitRef:   "abc123",
			},
		},
		{
			name: "failure status",
			info: &NotificationInfo{
				BuildID:  1002,
				Product:  "tidb",
				Version:  "v8.5.0",
				Status:   "failure",
				Platform: "linux/amd64",
				GitRef:   "abc123",
			},
		},
		{
			name: "error status",
			info: &NotificationInfo{
				BuildID:  1003,
				Product:  "tidb",
				Version:  "v8.5.0",
				Status:   "error",
				Platform: "linux/amd64",
				GitRef:   "abc123",
			},
		},
		{
			name: "aborted status",
			info: &NotificationInfo{
				BuildID:  1004,
				Product:  "tidb",
				Version:  "v8.5.0",
				Status:   "aborted",
				Platform: "linux/amd64",
				GitRef:   "abc123",
			},
		},
		{
			name: "processing status",
			info: &NotificationInfo{
				BuildID:  1005,
				Product:  "tidb",
				Version:  "v8.5.0",
				Status:   "processing",
				Platform: "linux/amd64",
				GitRef:   "abc123",
			},
		},
		{
			name: "with pipeline runs",
			info: &NotificationInfo{
				BuildID:  1006,
				Product:  "tidb",
				Version:  "v8.5.0",
				Status:   "success",
				Platform: "linux/amd64",
				GitRef:   "abc123",
				PipelineRuns: []PipelineRunInfo{
					{Name: "build-pipeline", Status: "Succeeded", URL: "https://tekton.example.com/run/1"},
					{Name: "test-pipeline", Status: "Failed", URL: "https://tekton.example.com/run/2"},
				},
			},
		},
		{
			name: "with error message",
			info: &NotificationInfo{
				BuildID:  1007,
				Product:  "tidb",
				Version:  "v8.5.0",
				Status:   "error",
				Platform: "linux/amd64",
				GitRef:   "abc123",
				ErrMsg:   "compilation failed: missing dependency",
			},
		},
		{
			name: "with github repo",
			info: &NotificationInfo{
				BuildID:    1008,
				Product:    "tidb",
				Version:    "v8.5.0",
				Status:     "success",
				Platform:   "linux/amd64",
				GitRef:     "abc123",
				GithubRepo: "pingcap/tidb",
			},
		},
		{
			name: "full info",
			info: &NotificationInfo{
				BuildID:    1009,
				Product:    "tidb",
				Version:    "v8.5.0",
				Status:     "success",
				CreatedBy:  "testuser",
				GitRef:     "abc123",
				GithubRepo: "pingcap/tidb",
				Platform:   "linux/amd64",
				ErrMsg:     "",
				PipelineRuns: []PipelineRunInfo{
					{Name: "build", Status: "Succeeded", URL: "https://tekton.example.com/run/1"},
				},
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			jsonStr, err := NewLarkCardJSON(tt.info)
			if (err != nil) != tt.wantErr {
				t.Fatalf("NewLarkCardJSON() error = %v, wantErr %v", err, tt.wantErr)
			}
			if tt.wantErr {
				return
			}

			var result map[string]any
			if err := json.Unmarshal([]byte(jsonStr), &result); err != nil {
				t.Fatalf("NewLarkCardJSON() returned invalid JSON: %v", err)
			}

			card, ok := result["card"].(map[string]any)
			if !ok {
				t.Fatal("missing 'card' key in result")
			}

			header, ok := card["header"].(map[string]any)
			if !ok {
				t.Fatal("missing 'header' key in card")
			}

			wantColor := StatusColor(tt.info.Status)
			if got := header["template"]; got != wantColor {
				t.Errorf("header.template = %v, want %v", got, wantColor)
			}
		})
	}
}

func TestNewLarkCardWithGoTemplate(t *testing.T) {
	info := &NotificationInfo{
		BuildID:  2001,
		Product:  "tidb",
		Version:  "v8.5.0",
		Status:   "success",
		Platform: "linux/amd64",
		GitRef:   "abc123",
	}

	card, err := NewLarkCardWithGoTemplate(info)
	if err != nil {
		t.Fatalf("NewLarkCardWithGoTemplate() unexpected error: %v", err)
	}

	cardMap, ok := card["card"].(map[string]any)
	if !ok {
		t.Fatal("missing 'card' key")
	}

	header, ok := cardMap["header"].(map[string]any)
	if !ok {
		t.Fatal("missing 'header' key in card")
	}

	if got := header["template"]; got != "green" {
		t.Errorf("header.template = %v, want green", got)
	}

	title, ok := header["title"].(map[string]any)
	if !ok {
		t.Fatal("missing 'title' key in header")
	}

	wantTitle := "✅ DevBuild #2001 - tidb v8.5.0"
	if got := title["content"]; got != wantTitle {
		t.Errorf("title.content = %v, want %v", got, wantTitle)
	}
}

func TestStatusColor(t *testing.T) {
	tests := []struct {
		status string
		want   string
	}{
		{"success", "green"},
		{"failure", "red"},
		{"error", "red"},
		{"aborted", "orange"},
		{"processing", "blue"},
		{"running", "blue"},
		{"unknown", "grey"},
		{"", "grey"},
	}

	for _, tt := range tests {
		if got := StatusColor(tt.status); got != tt.want {
			t.Errorf("StatusColor(%q) = %q, want %q", tt.status, got, tt.want)
		}
	}
}

func TestStatusEmoji(t *testing.T) {
	tests := []struct {
		status string
		want   string
	}{
		{"success", "✅"},
		{"failure", "❌"},
		{"error", "❌"},
		{"aborted", "🚫"},
		{"processing", "🔄"},
		{"running", "🔄"},
		{"unknown", "⏳"},
		{"", "⏳"},
	}

	for _, tt := range tests {
		if got := StatusEmoji(tt.status); got != tt.want {
			t.Errorf("StatusEmoji(%q) = %q, want %q", tt.status, got, tt.want)
		}
	}
}
