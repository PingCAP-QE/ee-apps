package controller

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/entity"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/database"
)

type RequestRotationRequestStruct struct {
	PipelineBuildId int `form:"pipeline_build_id"`
}

func RequestRotation(c *gin.Context) {
	params := RequestRotationRequestStruct{}
	if err := c.ShouldBindWith(&params, binding.Form); err != nil {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"code": 400, "message": "请求失败", "data": nil})
		return
	}

	var pipelineShow entity.PipelinesListShow
	if database.DBConn.DB.Where(&entity.PipelinesListShow{PipelineBuildId: params.PipelineBuildId}).First(&pipelineShow).Error != nil {
		c.AbortWithStatusJSON(http.StatusNotFound, gin.H{"code": 404, "message": "没有这个流水线id", "data": nil})
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": 200, "message": "请求成功", "data": gin.H{"status": pipelineShow.Status}})
}
