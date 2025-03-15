// Code generated by ent, DO NOT EDIT.

package ent

import (
	"time"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
)

// The init function reads all schema descriptors with runtime code
// (default values, validators, hooks and policies) and stitches it
// to their package variables.
func init() {
	devbuildMixin := schema.DevBuild{}.Mixin()
	devbuildMixinFields0 := devbuildMixin[0].Fields()
	_ = devbuildMixinFields0
	devbuildMixinFields1 := devbuildMixin[1].Fields()
	_ = devbuildMixinFields1
	devbuildMixinFields2 := devbuildMixin[2].Fields()
	_ = devbuildMixinFields2
	devbuildFields := schema.DevBuild{}.Fields()
	_ = devbuildFields
	// devbuildDescCreatedBy is the schema descriptor for created_by field.
	devbuildDescCreatedBy := devbuildMixinFields0[0].Descriptor()
	// devbuild.CreatedByValidator is a validator for the "created_by" field. It is called by the builders before save.
	devbuild.CreatedByValidator = devbuildDescCreatedBy.Validators[0].(func(string) error)
	// devbuildDescCreatedAt is the schema descriptor for created_at field.
	devbuildDescCreatedAt := devbuildMixinFields0[1].Descriptor()
	// devbuild.DefaultCreatedAt holds the default value on creation for the created_at field.
	devbuild.DefaultCreatedAt = devbuildDescCreatedAt.Default.(func() time.Time)
	// devbuildDescUpdatedAt is the schema descriptor for updated_at field.
	devbuildDescUpdatedAt := devbuildMixinFields0[2].Descriptor()
	// devbuild.DefaultUpdatedAt holds the default value on creation for the updated_at field.
	devbuild.DefaultUpdatedAt = devbuildDescUpdatedAt.Default.(func() time.Time)
	// devbuild.UpdateDefaultUpdatedAt holds the default value on update for the updated_at field.
	devbuild.UpdateDefaultUpdatedAt = devbuildDescUpdatedAt.UpdateDefault.(func() time.Time)
	// devbuildDescProduct is the schema descriptor for product field.
	devbuildDescProduct := devbuildMixinFields1[0].Descriptor()
	// devbuild.ProductValidator is a validator for the "product" field. It is called by the builders before save.
	devbuild.ProductValidator = devbuildDescProduct.Validators[0].(func(string) error)
	// devbuildDescEdition is the schema descriptor for edition field.
	devbuildDescEdition := devbuildMixinFields1[1].Descriptor()
	// devbuild.EditionValidator is a validator for the "edition" field. It is called by the builders before save.
	devbuild.EditionValidator = devbuildDescEdition.Validators[0].(func(string) error)
	// devbuildDescVersion is the schema descriptor for version field.
	devbuildDescVersion := devbuildMixinFields1[2].Descriptor()
	// devbuild.VersionValidator is a validator for the "version" field. It is called by the builders before save.
	devbuild.VersionValidator = devbuildDescVersion.Validators[0].(func(string) error)
	// devbuildDescGithubRepo is the schema descriptor for github_repo field.
	devbuildDescGithubRepo := devbuildMixinFields1[3].Descriptor()
	// devbuild.GithubRepoValidator is a validator for the "github_repo" field. It is called by the builders before save.
	devbuild.GithubRepoValidator = devbuildDescGithubRepo.Validators[0].(func(string) error)
	// devbuildDescGitRef is the schema descriptor for git_ref field.
	devbuildDescGitRef := devbuildMixinFields1[4].Descriptor()
	// devbuild.GitRefValidator is a validator for the "git_ref" field. It is called by the builders before save.
	devbuild.GitRefValidator = devbuildDescGitRef.Validators[0].(func(string) error)
	// devbuildDescGitSha is the schema descriptor for git_sha field.
	devbuildDescGitSha := devbuildMixinFields1[5].Descriptor()
	// devbuild.GitShaValidator is a validator for the "git_sha" field. It is called by the builders before save.
	devbuild.GitShaValidator = devbuildDescGitSha.Validators[0].(func(string) error)
	// devbuildDescPluginGitRef is the schema descriptor for plugin_git_ref field.
	devbuildDescPluginGitRef := devbuildMixinFields1[6].Descriptor()
	// devbuild.PluginGitRefValidator is a validator for the "plugin_git_ref" field. It is called by the builders before save.
	devbuild.PluginGitRefValidator = devbuildDescPluginGitRef.Validators[0].(func(string) error)
	// devbuildDescIsHotfix is the schema descriptor for is_hotfix field.
	devbuildDescIsHotfix := devbuildMixinFields1[7].Descriptor()
	// devbuild.DefaultIsHotfix holds the default value on creation for the is_hotfix field.
	devbuild.DefaultIsHotfix = devbuildDescIsHotfix.Default.(bool)
	// devbuildDescTargetImg is the schema descriptor for target_img field.
	devbuildDescTargetImg := devbuildMixinFields1[9].Descriptor()
	// devbuild.TargetImgValidator is a validator for the "target_img" field. It is called by the builders before save.
	devbuild.TargetImgValidator = devbuildDescTargetImg.Validators[0].(func(string) error)
	// devbuildDescPipelineEngine is the schema descriptor for pipeline_engine field.
	devbuildDescPipelineEngine := devbuildMixinFields1[10].Descriptor()
	// devbuild.DefaultPipelineEngine holds the default value on creation for the pipeline_engine field.
	devbuild.DefaultPipelineEngine = devbuildDescPipelineEngine.Default.(string)
	// devbuild.PipelineEngineValidator is a validator for the "pipeline_engine" field. It is called by the builders before save.
	devbuild.PipelineEngineValidator = devbuildDescPipelineEngine.Validators[0].(func(string) error)
	// devbuildDescBuilderImg is the schema descriptor for builder_img field.
	devbuildDescBuilderImg := devbuildMixinFields1[11].Descriptor()
	// devbuild.BuilderImgValidator is a validator for the "builder_img" field. It is called by the builders before save.
	devbuild.BuilderImgValidator = devbuildDescBuilderImg.Validators[0].(func(string) error)
	// devbuildDescBuildEnv is the schema descriptor for build_env field.
	devbuildDescBuildEnv := devbuildMixinFields1[12].Descriptor()
	// devbuild.BuildEnvValidator is a validator for the "build_env" field. It is called by the builders before save.
	devbuild.BuildEnvValidator = devbuildDescBuildEnv.Validators[0].(func(string) error)
	// devbuildDescFeatures is the schema descriptor for features field.
	devbuildDescFeatures := devbuildMixinFields1[13].Descriptor()
	// devbuild.FeaturesValidator is a validator for the "features" field. It is called by the builders before save.
	devbuild.FeaturesValidator = devbuildDescFeatures.Validators[0].(func(string) error)
	// devbuildDescProductBaseImg is the schema descriptor for product_base_img field.
	devbuildDescProductBaseImg := devbuildMixinFields1[14].Descriptor()
	// devbuild.ProductBaseImgValidator is a validator for the "product_base_img" field. It is called by the builders before save.
	devbuild.ProductBaseImgValidator = devbuildDescProductBaseImg.Validators[0].(func(string) error)
	// devbuildDescProductDockerfile is the schema descriptor for product_dockerfile field.
	devbuildDescProductDockerfile := devbuildMixinFields1[15].Descriptor()
	// devbuild.ProductDockerfileValidator is a validator for the "product_dockerfile" field. It is called by the builders before save.
	devbuild.ProductDockerfileValidator = devbuildDescProductDockerfile.Validators[0].(func(string) error)
	// devbuildDescStatus is the schema descriptor for status field.
	devbuildDescStatus := devbuildMixinFields2[0].Descriptor()
	// devbuild.DefaultStatus holds the default value on creation for the status field.
	devbuild.DefaultStatus = devbuildDescStatus.Default.(string)
	// devbuild.StatusValidator is a validator for the "status" field. It is called by the builders before save.
	devbuild.StatusValidator = devbuildDescStatus.Validators[0].(func(string) error)
	// devbuildDescErrMsg is the schema descriptor for err_msg field.
	devbuildDescErrMsg := devbuildMixinFields2[1].Descriptor()
	// devbuild.ErrMsgValidator is a validator for the "err_msg" field. It is called by the builders before save.
	devbuild.ErrMsgValidator = devbuildDescErrMsg.Validators[0].(func(string) error)
}
