package controller

import (
	"net/http"
	"strconv"
	"tibuild/commons/database"
	"tibuild/internalpkg/entity"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"
)

type RequestRotationRequestStruct struct {
	PipelineBuildId string `form:"pipeline_build_id"`
}

func RequestRotation(c *gin.Context) {
	// 获取类-每个构建类型下的流水线
	params := RequestRotationRequestStruct{}
	if err := c.ShouldBindWith(&params, binding.Form); err != nil {
		c.Error(err)
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "请求失败",
			"data":    nil,
		})
		return
	}

	var pipelines_list_show []entity.PipelinesListShow
	pipelineBuildId, _ := strconv.Atoi(params.PipelineBuildId)
	database.DBConn.DB.Where(&entity.PipelinesListShow{PipelineBuildId: pipelineBuildId}).Find(&pipelines_list_show)

	status := "程序异常"
	if len(pipelines_list_show) == 1 {
		status = pipelines_list_show[0].Status
	} else if len(pipelines_list_show) == 0 {
		println("没有这个流水线id ", params.PipelineBuildId)
	} else {
		println("流水线id不唯一 ", params.PipelineBuildId)
	}

	// 成功返回
	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data": gin.H{
			"status": status,
		},
	})

}
