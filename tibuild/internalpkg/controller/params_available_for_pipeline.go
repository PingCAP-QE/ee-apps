package controller

import (
	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"
	"net/http"
	"strings"
	"tibuild/commons/database"
)

type ParamsAvailableForPipelineStruct struct {
	PipelineId int `form:"pipeline_id"`
}

func ParamsAvailableForPipeline(c *gin.Context) {
	// 获取类-每个构建类型下的流水线
	params := ParamsAvailableForPipelineStruct{}
	if err := c.ShouldBindWith(&params, binding.Form); err != nil {
		c.Error(err)
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "请求失败",
			"data":    nil,
		})
		return
	}

	var tibuild_info []TibuildInfo
	println("params.PipelineId: ", params.PipelineId)
	database.DBConn.DB.Where(&TibuildInfo{PipelineId: params.PipelineId}).Find(&tibuild_info)
	var component []map[string]interface{}

	for _, value := range tibuild_info {
		available_params := map[string]interface{}{
			"build_type":    value.BuildType,
			"tab":           value.TabName,
			"component":     strings.Split(value.Component, ","),
			"branch":        strings.Split(value.Branch, ","),
			"version":       [1]string{value.Version},
			"arch":          strings.Split(value.Arch, ","),
			"artifact_type": strings.Split(value.ArtifactType, ","),
			"push_gcr":      strings.Split(value.PushGCR, ","),
		}
		component = append(component, available_params)
	}

	// 成功返回
	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    component,
	})

}
