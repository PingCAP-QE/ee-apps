package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestLarkNotifier_Notify_Disabled(t *testing.T) {
	notifier := NewLarkNotifier("http://example.com/webhook", false)
	build := &DevBuild{
		ID: 1,
		Spec: DevBuildSpec{
			Product: ProductTidb,
			Version: "v7.5.0",
		},
		Status: DevBuildStatus{
			Status: BuildStatusSuccess,
		},
	}

	err := notifier.Notify(context.Background(), build)
	assert.NoError(t, err)
}

func TestLarkNotifier_Notify_Success(t *testing.T) {
	var receivedMsg LarkCardMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		err := json.NewDecoder(r.Body).Decode(&receivedMsg)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	notifier := NewLarkNotifier(server.URL, true)
	build := &DevBuild{
		ID: 1,
		Spec: DevBuildSpec{
			Product:  ProductTidb,
			Version:  "v7.5.0",
			Platform: LinuxAmd64,
			IsHotfix: false,
		},
		Meta: DevBuildMeta{
			CreatedBy: "testuser",
		},
		Status: DevBuildStatus{
			Status:          BuildStatusSuccess,
			PipelineViewURLs: []string{"http://tekton.example.com/pipeline1"},
		},
	}

	err := notifier.Notify(context.Background(), build)
	require.NoError(t, err)

	assert.Equal(t, "interactive", receivedMsg.MsgType)
	assert.Equal(t, "green", receivedMsg.Card.Header.Template)
	assert.Contains(t, receivedMsg.Card.Header.Title.Content, "SUCCESS")
	assert.Contains(t, receivedMsg.Card.Header.Title.Content, "tidb")
	assert.Contains(t, receivedMsg.Card.Header.Title.Content, "v7.5.0")
}

func TestLarkNotifier_Notify_Failure(t *testing.T) {
	var receivedMsg LarkCardMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		err := json.NewDecoder(r.Body).Decode(&receivedMsg)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	notifier := NewLarkNotifier(server.URL, true)
	build := &DevBuild{
		ID: 1,
		Spec: DevBuildSpec{
			Product: ProductTikv,
			Version: "v8.0.0",
		},
		Status: DevBuildStatus{
			Status: BuildStatusFailure,
		},
	}

	err := notifier.Notify(context.Background(), build)
	require.NoError(t, err)

	assert.Equal(t, "red", receivedMsg.Card.Header.Template)
	assert.Contains(t, receivedMsg.Card.Header.Title.Content, "FAILURE")
}

func TestLarkNotifier_Notify_ServerError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	notifier := NewLarkNotifier(server.URL, true)
	build := &DevBuild{
		ID: 1,
		Spec: DevBuildSpec{
			Product: ProductTidb,
			Version: "v7.5.0",
		},
		Status: DevBuildStatus{
			Status: BuildStatusSuccess,
		},
	}

	err := notifier.Notify(context.Background(), build)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "unexpected status code: 500")
}

func TestStatusToTemplate(t *testing.T) {
	tests := []struct {
		status   string
		expected string
	}{
		{BuildStatusSuccess, "green"},
		{BuildStatusFailure, "red"},
		{BuildStatusError, "red"},
		{BuildStatusAborted, "orange"},
		{BuildStatusPending, "blue"},
		{BuildStatusProcessing, "blue"},
	}

	for _, tt := range tests {
		t.Run(tt.status, func(t *testing.T) {
			result := StatusToTemplate(tt.status)
			assert.Equal(t, tt.expected, result)
		})
	}
}

func TestStatusToEmoji(t *testing.T) {
	tests := []struct {
		status   string
		expected string
	}{
		{BuildStatusSuccess, "✅"},
		{BuildStatusFailure, "❌"},
		{BuildStatusError, "⚠️"},
		{BuildStatusAborted, "🚫"},
		{BuildStatusPending, "⏳"},
		{BuildStatusProcessing, "⏳"},
	}

	for _, tt := range tests {
		t.Run(tt.status, func(t *testing.T) {
			result := StatusToEmoji(tt.status)
			assert.Equal(t, tt.expected, result)
		})
	}
}

func TestBuildLarkCard(t *testing.T) {
	info := &NotificationInfo{
		Product:   "tidb",
		Version:   "v7.5.0",
		Status:    BuildStatusSuccess,
		Platform:  LinuxAmd64,
		ViewURLs:  []string{"http://tekton.example.com/pipeline1"},
		CreatedBy: "testuser",
		BuildID:   123,
		IsHotfix:  false,
	}

	card, err := buildLarkCard(info)
	require.NoError(t, err)

	assert.Equal(t, "green", card.Header.Template)
	assert.Contains(t, card.Header.Title.Content, "✅")
	assert.Contains(t, card.Header.Title.Content, "SUCCESS")
	assert.NotEmpty(t, card.Elements)
}

func TestLarkNotifier_Notify_Hotfix(t *testing.T) {
	var receivedMsg LarkCardMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		err := json.NewDecoder(r.Body).Decode(&receivedMsg)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	notifier := NewLarkNotifier(server.URL, true)
	build := &DevBuild{
		ID: 1,
		Spec: DevBuildSpec{
			Product:  ProductTidb,
			Version:  "v7.5.0-20240101",
			IsHotfix: true,
		},
		Status: DevBuildStatus{
			Status: BuildStatusSuccess,
		},
	}

	err := notifier.Notify(context.Background(), build)
	require.NoError(t, err)

	assert.Equal(t, "green", receivedMsg.Card.Header.Template)
}

func TestLarkNotifier_Notify_ContextCanceled(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	notifier := NewLarkNotifier(server.URL, true)
	build := &DevBuild{
		ID: 1,
		Spec: DevBuildSpec{
			Product: ProductTidb,
			Version: "v7.5.0",
		},
		Status: DevBuildStatus{
			Status: BuildStatusSuccess,
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := notifier.Notify(ctx, build)
	assert.Error(t, err)
}
