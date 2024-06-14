package service

import (
	"context"
	"fmt"
	"strings"

	"github.com/bndr/gojenkins"
)

func Job_Status(jenkins *gojenkins.Jenkins, ctx context.Context, jobname string) {
	jobarr := strings.Split(jobname, "/")
	newjobname := strings.Join(jobarr, "/job/")
	job, err := jenkins.GetJob(ctx, newjobname)
	if err != nil {
		panic(err)
	}
	lastjob, _ := job.GetLastBuild(ctx)
	//lastjob.GetParameters()
	fmt.Println(lastjob.GetResult())
}
