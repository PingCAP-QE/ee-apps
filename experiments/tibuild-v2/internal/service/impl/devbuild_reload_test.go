package impl

import (
	"bytes"
	"testing"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
)

func TestReloadUpdatesTektonURLs(t *testing.T) {
	logger := zerolog.Nop()
	s := &devbuildsrvc{logger: &logger}

	s.Reload(&config.Service{Tekton: config.Tekton{
		APIURL:  "http://api.example.com/",
		ViewURL: "https://view.example.com/",
	}})
	require.Equal(t, "http://api.example.com", s.dashboardAPIURL)
	require.Equal(t, "https://view.example.com", s.dashboardViewURL)

	s.Reload(&config.Service{Tekton: config.Tekton{
		ViewURL: "https://view-only.example.com/",
	}})
	require.Equal(t, "https://view-only.example.com", s.dashboardAPIURL)
	require.Equal(t, "https://view-only.example.com", s.dashboardViewURL)
}

func TestLogTektonURLs(t *testing.T) {
	var buf bytes.Buffer
	logger := zerolog.New(&buf)

	logTektonURLs(&logger, &config.Service{Tekton: config.Tekton{
		APIURL:  "http://api.example.com",
		ViewURL: "https://view.example.com",
	}})
	require.Contains(t, buf.String(), "tekton dashboard URLs configured")
	require.NotContains(t, buf.String(), "falling back")

	buf.Reset()
	logTektonURLs(&logger, &config.Service{Tekton: config.Tekton{ViewURL: "https://view.example.com"}})
	require.Contains(t, buf.String(), "falling back to view_url")

	buf.Reset()
	logTektonURLs(&logger, &config.Service{Tekton: config.Tekton{APIURL: "http://api.example.com"}})
	require.Contains(t, buf.String(), "falling back to api_url")

	buf.Reset()
	logTektonURLs(&logger, &config.Service{})
	require.Contains(t, buf.String(), "reconciler is disabled")
}
