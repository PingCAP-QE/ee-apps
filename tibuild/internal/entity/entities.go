package entity

type PipelinesListShow struct {
	PipelineId      int    `gorm:"pipeline_id" yaml:"pipeline_id,omitempty" json:"pipeline_id,omitempty"`
	PipelineName    string `gorm:"pipeline_name" yaml:"pipeline_name,omitempty" json:"pipeline_name,omitempty"`
	PipelineBuildId int    `gorm:"primary_key;type:bigint(20) auto_increment;not null;comment:'ID';" json:"pipeline_build_id,omitempty" yaml:"pipeline_build_id,omitempty"`
	Status          string `gorm:"status" yaml:"status,omitempty" json:"status,omitempty"`
	Branch          string `gorm:"branch" yaml:"branch,omitempty" json:"branch,omitempty"`
	BuildType       string `gorm:"build_type" yaml:"build_type,omitempty" json:"build_type,omitempty"`
	Version         string `gorm:"version" yaml:"version,omitempty" json:"version,omitempty"`
	Arch            string `gorm:"arch" yaml:"arch,omitempty" json:"arch,omitempty"`
	Component       string `gorm:"component" yaml:"component,omitempty" json:"component,omitempty"`
	BeginTime       string `gorm:"begin_time" yaml:"begin_time,omitempty" json:"begin_time,omitempty"`
	EndTime         string `gorm:"end_time" yaml:"end_time,omitempty" json:"end_time,omitempty"`
	ArtifactType    string `gorm:"artifact_type" yaml:"artifact_type,omitempty" json:"artifact_type,omitempty"`
	ArtifactMeta    string `gorm:"artifact_meta" yaml:"artifact_meta,omitempty" json:"artifact_meta,omitempty"`
	PushGCR         string `gorm:"push_gcr" yaml:"push_gcr,omitempty" json:"push_gcr,omitempty"`
	JenkinsLog      string `gorm:"jenkins_log" yaml:"jenkins_log,omitempty" json:"jenkins_log,omitempty"`
	TriggeredBy     string `gorm:"triggered_by" yaml:"triggered_by,omitempty" json:"triggered_by,omitempty"`
}

func (PipelinesListShow) TableName() string {
	return "pipelines_list_show"
}
