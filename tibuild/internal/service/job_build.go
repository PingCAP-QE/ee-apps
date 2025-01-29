package service

import (
	"context"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"

	"github.com/bndr/gojenkins"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/entity"
)

func JobBuild(jenkins *gojenkins.Jenkins, ctx context.Context, jobname string, params map[string]string, pipeline_build_id int64) {
	jobarr := strings.Split(jobname, "/")
	newjobname := strings.TrimRight(strings.Join(jobarr, "/job/"), "/job/")
	joblist := strings.Split(newjobname, "/job/")

	qid, err := jenkins.BuildJob(ctx, newjobname, params)
	if err != nil {
		panic(err)
	}

	build, err := jenkins.GetBuildFromQueueID(ctx, qid)
	if err != nil {
		log.Println(err)
	}

	var jenkins_log string
	if len(joblist) == 2 {
		jenkins_log = "https://cd.pingcap.net/blue/organizations/jenkins/" + jobarr[0] + "/detail/" + jobarr[1] + "/" + strconv.FormatInt(build.GetBuildNumber(), 10) + "/pipeline"
	} else {
		jenkins_log = "https://cd.pingcap.net/blue/organizations/jenkins/" + jobarr[0] + "/detail/" + jobarr[0] + "/" + strconv.FormatInt(build.GetBuildNumber(), 10) + "/pipeline"
	}

	err = database.DBConn.DB.Model(new(entity.PipelinesListShow)).Where("pipeline_build_id = ?", int(pipeline_build_id)).Update("jenkins_log", jenkins_log).Error
	if err != nil {
		panic(err)
	}

	for build.IsRunning(ctx) {
		time.Sleep(5000 * time.Millisecond)
		build.Poll(ctx)
	}

	fmt.Printf("build number %d with result: %v\n", build.GetBuildNumber(),
		build.GetResult())
}
