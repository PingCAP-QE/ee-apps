package controller

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database"
)

type PipelineForBuildTypeStruct struct {
	BuildType string `form:"build_type"`
}

func PipelineForBuildType(c *gin.Context) {
	// 获取类-每个构建类型下的流水线
	params := PipelineForBuildTypeStruct{}
	if err := c.ShouldBindWith(&params, binding.Form); err != nil {
		c.Error(err)
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "请求失败",
			"data":    nil,
		})
		return
	}
	var tibuildInfo []TibuildInfo

	database.DBConn.DB.Where(&TibuildInfo{BuildType: params.BuildType}).Find(&tibuildInfo)
	var pipelines []map[string]interface{}

	for _, value := range tibuildInfo {
		m := map[string]interface{}{
			"pipeline_id":   value.PipelineId,
			"pipeline_name": value.TabName,
		}

		pipelines = append(pipelines, m)
	}

	// 成功返回
	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    pipelines,
	})
}
