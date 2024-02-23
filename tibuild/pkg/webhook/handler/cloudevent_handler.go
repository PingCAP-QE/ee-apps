package handler

import (
	"log/slog"

	rest "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/webhook/service"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
)

type CloudEventHandler struct {
	svc service.CloudEventService
}

func NewHandler(ds rest.DevBuildService) CloudEventHandler {
	return CloudEventHandler{svc: service.NewDevBuildCEServer(ds)}
}

func (h CloudEventHandler) Receive(c *gin.Context) {
	p, err := cloudevents.NewHTTP()
	if err != nil {
		slog.Error("Failed to create protocol", "error", err)
	}

	ceh, err := cloudevents.NewHTTPReceiveHandler(c, p, h.svc.Handle)
	if err != nil {
		slog.Error("failed to create handler", "error", err)
	}
	ceh.ServeHTTP(c.Writer, c.Request)
}
