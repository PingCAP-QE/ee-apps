package controller

import (
	"fmt"
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"

	"github.com/PingCAP-QE/ee-apps/tibuild/commons/database"
	"github.com/PingCAP-QE/ee-apps/tibuild/internalpkg/entity"
)

type PipelinesListShowRequest struct {
	PipelineId int `form:"pipeline_id"`
	Page       int `form:"page"`
	PageSize   int `form:"page_size"`
}

func PipelinesShow(c *gin.Context) {
	params := PipelinesListShowRequest{}
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
	println("params.PipelineId : %v", params.PipelineId)
	database.DBConn.DB.Where(&entity.PipelinesListShow{PipelineId: params.PipelineId}).Order("begin_time desc").Find(&pipelines_list_show)

	var pipelines_info []map[string]interface{}

	for index, value := range pipelines_list_show {
		println(value.BeginTime)
		m := map[string]interface{}{
			"index":             index,
			"pipeline_id":       value.PipelineId,
			"pipeline_name":     value.PipelineName,
			"pipeline_build_id": value.PipelineBuildId,
			"status":            value.Status,
			"branch":            value.Branch,
			"build_type":        value.BuildType,
			"version":           value.Version,
			"arch":              value.Arch,
			"component":         value.Component,
			"begin_time":        value.BeginTime,
			"end_time":          value.EndTime,
			"artifact_type":     value.ArtifactType,
			"push_gcr":          value.PushGCR,
			"artifact_meta":     value.ArtifactMeta,
			"jenkins_log":       value.JenkinsLog,
			"triggered_by":      value.TriggeredBy,
		}

		pipelines_info = append(pipelines_info, m)

	}
	fmt.Printf("result的类型是%T", pipelines_info)

	start_p := 0
	if params.Page > 1 {
		start_p = (params.Page - 1) * params.PageSize
	}

	end_p := 0
	if start_p+params.PageSize <= len(pipelines_info) {
		end_p = start_p + params.PageSize
	} else {
		end_p = len(pipelines_info)
	}

	println("all len : %d", len(pipelines_info))
	println("start position : %s", start_p)
	println("end position : %s", end_p)
	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    pipelines_info[start_p:end_p],
	})

}
