package controller

import (
	"errors"
	"net/http"
	"tibuild/pkg/rest/service"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"
)

type HTTPError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func bindParam(params interface{}, c *gin.Context) error {
	if err := c.ShouldBindWith(&params, binding.JSON); err != nil {
		c.JSON(http.StatusBadRequest, HTTPError{
			Code:    400,
			Message: "参数解析错误",
		})
		return err
	}
	return nil
}

func errorToCode(err error) int {
	if errors.Is(err, service.ErrNotFound) {
		return 404
	} else if errors.Is(err, service.ErrBadRequest) {
		return 400
	} else if errors.Is(err, service.ErrServerRefuse) {
		return 422
	} else {
		return 500
	}
}

func respondError(c *gin.Context, err error) {
	c.JSON(errorToCode(err), HTTPError{
		Code:    errorToCode(err),
		Message: err.Error(),
	})
}
