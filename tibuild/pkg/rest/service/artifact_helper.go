package service

import (
	"context"
	"fmt"
	"regexp"
)

type ArtifactHelper struct {
	jenkins Jenkins
}

const ImageSyncJobName = "jenkins-image-syncer"

func (helper *ArtifactHelper) SyncImage(ctx context.Context, req ImageSyncRequest) (resp *ImageSyncRequest, err error) {
	if err := validateImageSync(req); err != nil {
		return nil, err
	}
	qid, err := helper.jenkins.BuildJob(ctx, ImageSyncJobName, map[string]string{"SOURCE_IMAGE": req.Source, "TARGET_IMAGE": req.Target})
	if err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrInternalError)
	}
	go func() {
		bid, err := helper.jenkins.GetBuildNumberFromQueueID(ctx, qid)
		if err != nil {
			fmt.Printf("get build id from jenkins error: %+v", err)
			return
		}
		fmt.Printf("build id for sync to %s: is %d", req.Target, bid)
	}()
	return &req, nil
}

func validateImageSync(req ImageSyncRequest) error {
	if !source_image_reg.MatchString(req.Source) {
		return fmt.Errorf("source image not valid, must be %s%w", source_image_reg.String(), ErrBadRequest)
	}
	if !target_image_reg.MatchString(req.Target) {
		return fmt.Errorf("target image not valid, must be %s%w", target_image_reg.String(), ErrBadRequest)
	}
	return nil
}

var source_image_reg *regexp.Regexp
var target_image_reg *regexp.Regexp

func init() {
	source_image_reg = regexp.MustCompile(`^hub\.pingcap\.net/(qa|pingcap|tikv)/[\w-/]+:v\d+\.\d+\.\d+-\d{8,}.*$`)
	target_image_reg = regexp.MustCompile(`^(docker\.io/)?pingcap/[\w-]+:v\d+\.\d+\.\d+-\d{8,}.*$`)
}

func NewArtifactHelper(jenkins Jenkins) *ArtifactHelper {
	return &ArtifactHelper{jenkins: jenkins}
}
