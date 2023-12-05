package entity

type PipelinesListShow struct {
	PipelineId      int    `gorm:"pipeline_id"`
	PipelineName    string `gorm:"pipeline_name"`
	PipelineBuildId int    `gorm:"primary_key;type:bigint(20) auto_increment;not null;comment:'ID';" json:"pipeline_build_id"`
	Status          string `gorm:"status"`
	Branch          string `gorm:"branch"`
	BuildType       string `gorm:"build_type"`
	Version         string `gorm:"version"`
	Arch            string `gorm:"arch"`
	Component       string `gorm:"component"`
	BeginTime       string `gorm:"begin_time"`
	EndTime         string `gorm:"end_time"`
	ArtifactType    string `gorm:"artifact_type"`
	ArtifactMeta    string `gorm:"artifact_meta"`
	PushGCR         string `gorm:"push_gcr"`
	JenkinsLog      string `gorm:"jenkins_log"`
	TriggeredBy     string `gorm:"triggered_by"`
}

func (PipelinesListShow) TableName() string {
	return "pipelines_list_show"
}
