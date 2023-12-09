package controller

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/PingCAP-QE/ee-apps/tibuild/commons/configs"
	"github.com/PingCAP-QE/ee-apps/tibuild/commons/database"
	"github.com/PingCAP-QE/ee-apps/tibuild/gojenkins"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/entity"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service"
)

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
	log.Println(c.Request.URL.Path)

	var params PipelineTriggerStruct
	if err := c.Bind(&params); err != nil {
		return
	}
	println(params.PipelineId)
	println(params.TriggeredBy)
	println(params.ArtifactType)
	println(params.Arch)
	println(params.Component)
	println(params.Branch)
	println(params.Version)

	// 查数据库关联
	var tibuildInfo []TibuildInfo
	database.DBConn.DB.Where(&TibuildInfo{PipelineId: params.PipelineId}).Find(&tibuildInfo)
	fmt.Printf("tibuildInfo : %+v \n", tibuildInfo)

	onlinePipList := [...]string{"rc-build", "ga-build", "hotfix-build"}
	for _, value := range onlinePipList {
		if tibuildInfo[0].BuildType == value {
			// 白名单查询
			var whileList []WhiteList
			database.DBConn.DB.Raw("select name from tibuild_white_list where name = ?", params.TriggeredBy).Scan(&whileList)
			println("在白名单中：%v", params.TriggeredBy)

			if len(whileList) <= 0 {
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
		PipelineName: tibuildInfo[0].TabName,
		Status:       "Processing",
		Branch:       params.Branch,
		BuildType:    tibuildInfo[0].BuildType,
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
	fmt.Println("pipeline_build_id : ", ps.PipelineBuildId)
	fmt.Println("begin_time: ", ps.BeginTime)

	params_trans := make(map[string]string)
	params_trans["PIPELINE_BUILD_ID"] = strconv.Itoa(ps.PipelineBuildId)
	log.Println(params_trans)

	go triggerJenkinsJob(ctx, &tibuildInfo[0], &params, params_trans, jenkins, int64(ps.PipelineBuildId))

	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    &TriggerRes{PipelineBuildId: ps.PipelineBuildId},
	})
}

func triggerJenkinsJob(ctx context.Context, tibuildInfo *TibuildInfo, params *PipelineTriggerStruct, params_trans map[string]string, jenkins *gojenkins.Jenkins, pipelineBuildID int64) {
	switch params.PipelineId {
	case 1, 2, 3, 4, 5, 6:
		fmt.Println("触发构建的是多分支流水线，没有传入参数")
	case 7:
		fmt.Println("Tab展示名：Nightly Image Build For QA")
		if params.Branch == "master" {
			params_trans["GIT_BRANCH"] = "master"
			params_trans["NEED_MULTIARCH"] = "true"
		} else {
			params_trans["GIT_BRANCH"] = params.Branch
			params_trans["NEED_MULTIARCH"] = "false"
		}
		params_trans["FORCE_REBUILD"] = "false"

	case 8:
		fmt.Println("Tab展示名：Nightly Image Build to Dockerhub")

	case 9:
		fmt.Println("Tab展示名：Nightly TiUP Build")
	case 10:
		params_trans["RELEASE_BRANCH"] = params.Branch
		params_trans["RELEASE_TAG"] = params.Version
	case 11:
		params_trans["RELEASE_TAG"] = params.Version
		params_trans["RELEASE_BRANCH"] = params.Branch
		params_trans["NEED_MULTIARCH"] = "true"
		params_trans["DEBUG_MODE"] = "false"
	case 12: // dev-build
		params_trans["PRODUCT"] = params.Component
		if params.Component == "ticdc" || params.Component == "dm" {
			params_trans["REPO"] = "tiflow"
		} else if params.Component == "tidb" || params.Component == "dumpling" || params.Component == "lightning" || params.Component == "br" {
			params_trans["REPO"] = "tidb"
		} else {
			params_trans["REPO"] = params.Component
		}
		params_trans["HOTFIX_TAG"] = params.Version
		if params.PushGCR == "Yes" {
			params_trans["PUSH_GCR"] = "true"
		} else {
			params_trans["PUSH_GCR"] = "false"
		}
		if params.ArtifactType == "enterprise image" {
			params_trans["PUSH_DOCKER_HUB"] = "false"
			params_trans["EDITION"] = "enterprise"
		} else {
			params_trans["PUSH_DOCKER_HUB"] = "true"
			params_trans["EDITION"] = "community"
		}
		params_trans["FORCE_REBUILD"] = "true"
		params_trans["DEBUG"] = "false"
		if params.Arch == "All" {
			params_trans["ARCH"] = "both"
		} else if params.Arch == "linux-amd64" {
			params_trans["ARCH"] = "amd64"
		} else {
			params_trans["ARCH"] = "arm64"
		}

	}

	job_name_tmp := strings.Split(tibuildInfo.Pipeline, "/job/")
	if len(job_name_tmp) <= 1 {
		println("Jenkins uri 非法！")
	} else if len(job_name_tmp) == 2 {
		job_name := strings.Split(job_name_tmp[1], "/")[0]
		println(job_name)
		service.Job_Build(jenkins, ctx, job_name, params_trans, pipelineBuildID)
	} else {
		job_name := job_name_tmp[1] + "/" + job_name_tmp[2]
		println(job_name)
		service.Job_Build(jenkins, ctx, job_name, params_trans, pipelineBuildID)
	}
}
