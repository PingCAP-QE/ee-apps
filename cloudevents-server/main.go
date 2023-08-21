package main

import (
	"fmt"
	"net/http"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"
)

func index(c *gin.Context) {
	c.JSON(http.StatusOK, "Welcome to CloudEvents")
}

func healthz(c *gin.Context) {
	c.String(http.StatusOK, "OK")
}

func cloudEventsHandler() gin.HandlerFunc {
	return func(c *gin.Context) {
		p, err := cloudevents.NewHTTP()
		if err != nil {
			log.Fatal().
				Err(err).
				Msg("Failed to create protocol")
		}

		h, err := cloudevents.NewHTTPReceiveHandler(c, p, receive)
		if err != nil {
			log.Fatal().
				Err(err).
				Msg("failed to create handler")
		}

		h.ServeHTTP(c.Writer, c.Request)
	}
}

func receive(event cloudevents.Event) cloudevents.Result {
	fmt.Printf("Got an Event: %s", event)
	return cloudevents.ResultACK
}

func main() {
	r := gin.Default()
	r.SetTrustedProxies(nil)

	r.GET("/", index)
	r.GET("/healthz", healthz)
	r.POST("/events", cloudEventsHandler())

	log.Fatal().
		Err(http.ListenAndServe(":8080", r))
}
