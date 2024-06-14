package controller

import (
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/entity"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/database"
)

type RequestResultRequestStruct struct {
	PipelineBuildId string `form:"pipeline_build_id"`
}

func RequestResult(c *gin.Context) {
	// 获取类-每个构建类型下的流水线
	params := RequestResultRequestStruct{}
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

	var data map[string]interface{}

	if len(pipelines_list_show) == 1 {
		data = map[string]interface{}{
			"pipeline_id":       pipelines_list_show[0].PipelineId,
			"pipeline_name":     pipelines_list_show[0].PipelineName,
			"pipeline_build_id": pipelines_list_show[0].PipelineBuildId,
			"status":            pipelines_list_show[0].Status,
			"branch":            pipelines_list_show[0].Branch,
			"build_type":        pipelines_list_show[0].BuildType,
			"version":           pipelines_list_show[0].Version,
			"arch":              pipelines_list_show[0].Arch,
			"component":         pipelines_list_show[0].Component,
			"begin_time":        pipelines_list_show[0].BeginTime,
			"end_time":          pipelines_list_show[0].EndTime,
			"artifact_type":     pipelines_list_show[0].ArtifactType,
			"push_gcr":          pipelines_list_show[0].PushGCR,
			"artifact_meta":     pipelines_list_show[0].ArtifactMeta,
			"jenkins_log":       pipelines_list_show[0].JenkinsLog,
			"triggered_by":      pipelines_list_show[0].TriggeredBy,
		}
	} else if len(pipelines_list_show) == 0 {
		println("没有这个流水线id ", params.PipelineBuildId)
	} else {
		println("流水线id不唯一 ", params.PipelineBuildId)
	}

	// 成功返回
	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    data,
	})

}
