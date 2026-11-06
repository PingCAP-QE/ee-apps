package config

import "testing"

func TestTektonBaseURLs(t *testing.T) {
	tests := []struct {
		name     string
		tekton   Tekton
		wantAPI  string
		wantView string
	}{
		{
			name:     "both empty",
			tekton:   Tekton{},
			wantAPI:  "",
			wantView: "",
		},
		{
			name:     "api url only",
			tekton:   Tekton{APIURL: "http://api.example.com"},
			wantAPI:  "http://api.example.com",
			wantView: "http://api.example.com",
		},
		{
			name:     "view url only",
			tekton:   Tekton{ViewURL: "https://view.example.com"},
			wantAPI:  "https://view.example.com",
			wantView: "https://view.example.com",
		},
		{
			name:     "both set",
			tekton:   Tekton{APIURL: "http://api.example.com", ViewURL: "https://view.example.com"},
			wantAPI:  "http://api.example.com",
			wantView: "https://view.example.com",
		},
		{
			name:     "trailing slashes trimmed",
			tekton:   Tekton{APIURL: "http://api.example.com/", ViewURL: "https://view.example.com/"},
			wantAPI:  "http://api.example.com",
			wantView: "https://view.example.com",
		},
		{
			name:     "only api url with trailing slash",
			tekton:   Tekton{APIURL: "http://api.example.com/"},
			wantAPI:  "http://api.example.com",
			wantView: "http://api.example.com",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tt.tekton.APIBaseURL(); got != tt.wantAPI {
				t.Fatalf("APIBaseURL() = %q, want %q", got, tt.wantAPI)
			}
			if got := tt.tekton.ViewBaseURL(); got != tt.wantView {
				t.Fatalf("ViewBaseURL() = %q, want %q", got, tt.wantView)
			}
		})
	}
}

func TestTektonReconcilerEnabledUsesAPIBaseURL(t *testing.T) {
	tests := []struct {
		name   string
		tekton Tekton
		want   bool
	}{
		{name: "disabled when both empty", tekton: Tekton{}, want: false},
		{name: "enabled by api url", tekton: Tekton{APIURL: "http://api.example.com"}, want: true},
		{name: "enabled by legacy view url", tekton: Tekton{ViewURL: "https://view.example.com"}, want: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tt.tekton.APIBaseURL() != ""; got != tt.want {
				t.Fatalf("reconciler enabled = %v, want %v", got, tt.want)
			}
		})
	}
}
