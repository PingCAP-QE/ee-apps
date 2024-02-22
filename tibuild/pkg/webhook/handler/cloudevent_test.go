package handler

import (
	"bytes"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/webhook/service"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

type MockHandler struct {
	Received cloudevents.Event
}

func (s *MockHandler) Handle(e cloudevents.Event) {
	s.Received = e
}

var _ service.CloudEventService = &MockHandler{}

func newCE() (CloudEventHandler, *MockHandler) {
	mock := &MockHandler{}
	return CloudEventHandler{svc: mock}, mock
}

func TestDevBuildGet(t *testing.T) {
	h, m := newCE()
	content, err := os.ReadFile("event.json")
	require.NoError(t, err)
	req := httptest.NewRequest(http.MethodPost, "/api/cloudevent", bytes.NewBuffer(content))
	req.Header.Add("Content-Type", "application/cloudevents+json; charset=utf-8")
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = req
	h.Receive(c)
	require.Equal(t, 200, w.Result().StatusCode)
	fmt.Printf("evnet is %v", m.Received)
}
