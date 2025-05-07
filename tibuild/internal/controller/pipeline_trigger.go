package controller

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bndr/gojenkins"
	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/entity"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/configs"
)

const (
	nightlyQAImageBuildPipelineID = 7
	nightlyImageBuildPipelineID   = 8
	nightlyTiupBuildPipelineID    = 9
	devBuildpipelineID            = 12
)

var componentToRepoMap = map[string]string{
	"ticdc":         "tiflow",
	"dm":            "tiflow",
	"tidb":          "tidb",
	"dumpling":      "tidb",
	"lightning":     "tidb",
	"br":            "tidb",
	"ticdc-newarch": "ticdc",
	"tiproxy":       "tiproxy",
}

type PipelineTriggerStruct struct {
	Arch         string `form:"arch" json:"arch" validate:"required"`
	ArtifactType string `form:"artifact_type" json:"artifact_type" validate:"required"`
	Branch       string `form:"branch" json:"branch" validate:"required"`
	Component    string `form:"component" json:"component" validate:"required"`
	PipelineId   int    `form:"pipeline_id" json:"pipeline_id" validate:"required,numeric"`
	Version      string `form:"version" json:"version" validate:"required,startswith=v"`
	TriggeredBy  string `form:"triggered_by" json:"triggered_by" validate:"required"`
	PushGCR      string `form:"push_gcr" json:"push_gcr" validate:"required"`
}

type TibuildInfo struct {
	PipelineId   int    `form:"pipeline_id"`
	BuildType    string `form:"build_type"`
	TabName      string `form:"tab_name"`
	Component    string `form:"component"`
	Branch       string `form:"branch"`
	Version      string `form:"version"`
	Arch         string `form:"arch"`
	PushGCR      string `form:"push_gcr"`
	ArtifactType string `form:"artifact_type"`
	Pipeline     string `form:"pipeline"`
	ArtifactMeta string `form:"artifact_meta"`
}

type TriggerRes struct {
	PipelineBuildId int `json:"pipeline_build_id" form:"pipeline_build_id" uri:"pipeline_build_id"`
}

func (TibuildInfo) TableName() string {
	return "tibuild_info"
}

type WhiteList struct {
	Id     int    `gorm:"id"`
	Name   string `gorm:"name"`
	NameCn string `gorm:"name_cn"`
}

func PipelineTrigger(c *gin.Context) {
	var params PipelineTriggerStruct
	if err := c.Bind(&params); err != nil {
		return
	}
	log.Debug().
		Int("pipeline_id", params.PipelineId).
		Str("triggered_by", params.TriggeredBy).
		Str("artifact_type", params.ArtifactType).
		Str("arch", params.Arch).
		Str("component", params.Component).
		Str("branch", params.Branch).
		Str("version", params.Version).
		Msg("received trigger request")

	// 查数据库关联
	var tibuildInfo TibuildInfo
	database.DBConn.DB.Where(&TibuildInfo{PipelineId: params.PipelineId}).Find(&tibuildInfo)
	if database.DBConn.DB.Where(&TibuildInfo{PipelineId: params.PipelineId}).First(&tibuildInfo).Error != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    404,
			"message": "build not found",
			"data":    params.TriggeredBy,
		})
		return
	}
	log.Debug().Any("data", &tibuildInfo).Msg("build info")

	onlinePipList := [...]string{"rc-build", "ga-build", "hotfix-build"}
	for _, value := range onlinePipList {
		if tibuildInfo.BuildType == value {
			// 白名单查询
			var whileListCount int64
			database.DBConn.DB.Raw("select name from tibuild_white_list where name = ?", params.TriggeredBy).Count(&whileListCount)

			if whileListCount <= 0 {
				c.JSON(http.StatusOK, gin.H{
					"code":    401,
					"message": "没有触发权限，白名单拦截",
					"data":    params.TriggeredBy,
				})
				return
			}
		}
	}

	// jenkins
	ctx := context.Background()
	jenkins, _ := gojenkins.CreateJenkins(nil, "https://cd.pingcap.net/", configs.Config.Jenkins.UserName, configs.Config.Jenkins.PassWord).Init(ctx)
	var cstSh, _ = time.LoadLocation("Asia/Shanghai") //上海

	ps := entity.PipelinesListShow{
		PipelineId:   params.PipelineId,
		PipelineName: tibuildInfo.TabName,
		Status:       "Processing",
		Branch:       params.Branch,
		BuildType:    tibuildInfo.BuildType,
		Version:      params.Version,
		Arch:         params.Arch,
		Component:    params.Component,
		BeginTime:    time.Now().In(cstSh).Format("2006-01-02 15:04:05"),
		EndTime:      "",
		ArtifactType: params.ArtifactType,
		ArtifactMeta: "",
		PushGCR:      params.PushGCR,
		JenkinsLog:   "",
		TriggeredBy:  params.TriggeredBy,
	}

	database.DBConn.DB.Debug().Create(&ps)
	log.Debug().Int("pipeline_build_id", ps.PipelineBuildId).Str("begin_time", ps.BeginTime).Send()

	params_trans := make(map[string]string)
	params_trans["PIPELINE_BUILD_ID"] = strconv.Itoa(ps.PipelineBuildId)

	go triggerJenkinsJob(ctx, &tibuildInfo, &params, params_trans, jenkins, int64(ps.PipelineBuildId))

	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    &TriggerRes{PipelineBuildId: ps.PipelineBuildId},
	})
}

func triggerJenkinsJob(ctx context.Context, tibuildInfo *TibuildInfo, params *PipelineTriggerStruct, params_trans map[string]string, jenkins *gojenkins.Jenkins, pipelineBuildID int64) {
	switch params.PipelineId {
	case nightlyQAImageBuildPipelineID:
		params_trans["NEED_MULTIARCH"] = strconv.FormatBool(params.Branch == "master")
		params_trans["FORCE_REBUILD"] = "false"
	case 10:
		params_trans["RELEASE_BRANCH"] = params.Branch
		params_trans["RELEASE_TAG"] = params.Version
	case 11:
		params_trans["RELEASE_TAG"] = params.Version
		params_trans["RELEASE_BRANCH"] = params.Branch
		params_trans["NEED_MULTIARCH"] = "true"
		params_trans["DEBUG_MODE"] = "false"
	case devBuildpipelineID:
		params_trans["PRODUCT"] = params.Component
		params_trans["REPO"] = componentToRepoMap[params.Component]
		params_trans["HOTFIX_TAG"] = params.Version
		params_trans["PUSH_GCR"] = strconv.FormatBool(params.PushGCR == "Yes")
		params_trans["PUSH_DOCKER_HUB"] = strconv.FormatBool(params.ArtifactType != "enterprise image")
		params_trans["EDITION"] = map[bool]string{true: "enterprise", false: "community"}[params.ArtifactType == "enterprise image"]
		params_trans["FORCE_REBUILD"] = "true"
		params_trans["DEBUG"] = "false"
		archMap := map[string]string{
			"All":         "both",
			"linux-amd64": "amd64",
		}
		params_trans["ARCH"] = archMap[params.Arch]
	}

	jobNameParts := strings.Split(tibuildInfo.Pipeline, "/job/")
	if len(jobNameParts) < 2 {
		log.Error().Str("pipeline_name", tibuildInfo.Pipeline).Msg("Invalid Jenkins URI!")
		return
	}

	jobName := strings.Split(jobNameParts[1], "/")[0]
	service.JobBuild(jenkins, ctx, jobName, params_trans, pipelineBuildID)
}
