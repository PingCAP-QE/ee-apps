package api

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"
)

// error return struct definition
type APIError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (err APIError) Error() string {
	return err.Message
}

// middleware error handler in server package
func APIErrorJSONReporter() gin.HandlerFunc {
	return APIErrorJSONReporterHandler(gin.ErrorTypeAny)
}

func APIErrorJSONReporterHandler(errType gin.ErrorType) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()
		innerErrors := c.Errors.ByType(errType)

		if len(innerErrors) > 0 {
			err := innerErrors[0].Err
			parsedError := &APIError{
				Code:    http.StatusInternalServerError,
				Message: err.Error(),
			}
			log.Error().Err(parsedError).Send()

			c.AbortWithStatusJSON(parsedError.Code, parsedError)
			return
		}

	}
}
