package service

import (
	"context"
	"fmt"

	"github.com/bndr/gojenkins"
)

type Jenkins interface {
	BuildJob(ctx context.Context, name string, params map[string]string) (int64, error)
	GetBuildNumberFromQueueID(ctx context.Context, queueid int64) (int64, error)
	GetBuild(ctx context.Context, jobName string, number int64) (*gojenkins.Build, error)
	BuildURL(jobName string, number int64) string
}

type JenkinsClient struct {
	Delegate *gojenkins.Jenkins
}

func (j JenkinsClient) BuildJob(ctx context.Context, name string, params map[string]string) (int64, error) {
	return j.Delegate.BuildJob(ctx, name, params)
}

func (j JenkinsClient) GetBuildNumberFromQueueID(ctx context.Context, queueid int64) (int64, error) {
	build, err := j.Delegate.GetBuildFromQueueID(ctx, queueid)
	if err != nil {
		return 0, err
	}
	return build.GetBuildNumber(), nil
}

func (j JenkinsClient) GetBuild(ctx context.Context, jobName string, number int64) (*gojenkins.Build, error) {
	return j.Delegate.GetBuild(ctx, jobName, number)
}

func (j JenkinsClient) BuildURL(jobName string, number int64) string {
	return fmt.Sprintf("%s/blue/organizations/jenkins/%s/detail/%s/%d/pipeline", j.Delegate.Server, jobName, jobName, number)
}

func NewJenkins(ctx context.Context, url string, username string, password string) (*JenkinsClient, error) {
	jenkins, err := gojenkins.CreateJenkins(nil, url, username, password).Init(ctx)
	if err != nil {
		return nil, err
	}
	return &JenkinsClient{Delegate: jenkins}, nil
}
