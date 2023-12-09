package service

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/commons/database"
	"github.com/PingCAP-QE/ee-apps/tibuild/gojenkins"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/entity"
)

func Job_Build(jenkins *gojenkins.Jenkins, ctx context.Context, jobname string, params map[string]string, pipeline_build_id int) {
	jobarr := strings.Split(jobname, "/")
	newjobname := strings.TrimRight(strings.Join(jobarr, "/job/"), "/job/")
	println("*************** 输入参数是 **************")
	println("job name: ", jobname)
	println("new job name: ", newjobname)
	println("***************************************")
	joblist := strings.Split(newjobname, "/job/")
	println(params["PIPELINE_BUILD_ID"])

	qid, err := jenkins.BuildJob(ctx, newjobname, params)

	if err != nil {
		panic(err)
	}
	println("qid : ", qid)
	build, err := jenkins.GetBuildFromQueueID(ctx, qid, jobname)
	println("Jenkins build number is : ", build.GetBuildNumber())

	var jenkins_log string
	if len(joblist) == 2 {
		jenkins_log = "https://cd.pingcap.net/blue/organizations/jenkins/" + jobarr[0] + "/detail/" + jobarr[1] + "/" + strconv.FormatInt(build.GetBuildNumber(), 10) + "/pipeline"
	} else {
		jenkins_log = "https://cd.pingcap.net/blue/organizations/jenkins/" + jobarr[0] + "/detail/" + jobarr[0] + "/" + strconv.FormatInt(build.GetBuildNumber(), 10) + "/pipeline"
	}

	err = database.DBConn.DB.Model(new(entity.PipelinesListShow)).Where("pipeline_build_id = ?", pipeline_build_id).Update("jenkins_log", jenkins_log).Error

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

func MapToJson(param map[string]interface{}) string {
	dataType, _ := json.Marshal(param)
	dataString := string(dataType)
	return dataString
}

func JsonToMap(str string) map[string]string {

	var tempMap map[string]string

	err := json.Unmarshal([]byte(str), &tempMap)

	if err != nil {
		panic(err)
	}

	return tempMap
}

func GetInterfaceToString(value interface{}) string {
	// interface 转 string
	var key string
	if value == nil {
		return key
	}

	switch val := value.(type) {
	case float64:
		key = strconv.FormatFloat(val, 'f', -1, 64)
	case float32:
		key = strconv.FormatFloat(float64(val), 'f', -1, 64)
	case int:
		key = strconv.Itoa(val)
	case uint:
		key = strconv.Itoa(int(val))
	case int8:
		it := value.(int8)
		key = strconv.Itoa(int(it))
	case uint8:
		key = strconv.Itoa(int(val))
	case int16:
		key = strconv.Itoa(int(val))
	case uint16:
		key = strconv.Itoa(int(val))
	case int32:
		key = strconv.Itoa(int(val))
	case uint32:
		key = strconv.Itoa(int(val))
	case int64:
		key = strconv.FormatInt(val, 10)
	case uint64:
		key = strconv.FormatUint(val, 10)
	case string:
		key = val
	case []byte:
		key = string(val)
	default:
		newValue, _ := json.Marshal(val)
		key = string(newValue)
	}

	return key
}
