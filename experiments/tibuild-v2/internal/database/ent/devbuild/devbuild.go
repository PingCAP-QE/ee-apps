// Code generated by ent, DO NOT EDIT.

package devbuild

import (
	"time"

	"entgo.io/ent/dialect/sql"
)

const (
	// Label holds the string label denoting the devbuild type in the database.
	Label = "dev_build"
	// FieldID holds the string denoting the id field in the database.
	FieldID = "id"
	// FieldCreatedBy holds the string denoting the created_by field in the database.
	FieldCreatedBy = "created_by"
	// FieldCreatedAt holds the string denoting the created_at field in the database.
	FieldCreatedAt = "created_at"
	// FieldUpdatedAt holds the string denoting the updated_at field in the database.
	FieldUpdatedAt = "updated_at"
	// FieldProduct holds the string denoting the product field in the database.
	FieldProduct = "product"
	// FieldEdition holds the string denoting the edition field in the database.
	FieldEdition = "edition"
	// FieldVersion holds the string denoting the version field in the database.
	FieldVersion = "version"
	// FieldGithubRepo holds the string denoting the github_repo field in the database.
	FieldGithubRepo = "github_repo"
	// FieldGitRef holds the string denoting the git_ref field in the database.
	FieldGitRef = "git_ref"
	// FieldGitHash holds the string denoting the git_hash field in the database.
	FieldGitHash = "git_hash"
	// FieldPluginGitRef holds the string denoting the plugin_git_ref field in the database.
	FieldPluginGitRef = "plugin_git_ref"
	// FieldIsHotfix holds the string denoting the is_hotfix field in the database.
	FieldIsHotfix = "is_hotfix"
	// FieldIsPushGcr holds the string denoting the is_push_gcr field in the database.
	FieldIsPushGcr = "is_push_gcr"
	// FieldTargetImg holds the string denoting the target_img field in the database.
	FieldTargetImg = "target_img"
	// FieldPipelineEngine holds the string denoting the pipeline_engine field in the database.
	FieldPipelineEngine = "pipeline_engine"
	// FieldBuilderImg holds the string denoting the builder_img field in the database.
	FieldBuilderImg = "builder_img"
	// FieldBuildEnv holds the string denoting the build_env field in the database.
	FieldBuildEnv = "build_env"
	// FieldFeatures holds the string denoting the features field in the database.
	FieldFeatures = "features"
	// FieldProductBaseImg holds the string denoting the product_base_img field in the database.
	FieldProductBaseImg = "product_base_img"
	// FieldProductDockerfile holds the string denoting the product_dockerfile field in the database.
	FieldProductDockerfile = "product_dockerfile"
	// FieldStatus holds the string denoting the status field in the database.
	FieldStatus = "status"
	// FieldErrMsg holds the string denoting the err_msg field in the database.
	FieldErrMsg = "err_msg"
	// FieldPipelineBuildID holds the string denoting the pipeline_build_id field in the database.
	FieldPipelineBuildID = "pipeline_build_id"
	// FieldPipelineStartAt holds the string denoting the pipeline_start_at field in the database.
	FieldPipelineStartAt = "pipeline_start_at"
	// FieldPipelineEndAt holds the string denoting the pipeline_end_at field in the database.
	FieldPipelineEndAt = "pipeline_end_at"
	// FieldBuildReport holds the string denoting the build_report field in the database.
	FieldBuildReport = "build_report"
	// FieldTektonStatus holds the string denoting the tekton_status field in the database.
	FieldTektonStatus = "tekton_status"
	// Table holds the table name of the devbuild in the database.
	Table = "dev_builds"
)

// Columns holds all SQL columns for devbuild fields.
var Columns = []string{
	FieldID,
	FieldCreatedBy,
	FieldCreatedAt,
	FieldUpdatedAt,
	FieldProduct,
	FieldEdition,
	FieldVersion,
	FieldGithubRepo,
	FieldGitRef,
	FieldGitHash,
	FieldPluginGitRef,
	FieldIsHotfix,
	FieldIsPushGcr,
	FieldTargetImg,
	FieldPipelineEngine,
	FieldBuilderImg,
	FieldBuildEnv,
	FieldFeatures,
	FieldProductBaseImg,
	FieldProductDockerfile,
	FieldStatus,
	FieldErrMsg,
	FieldPipelineBuildID,
	FieldPipelineStartAt,
	FieldPipelineEndAt,
	FieldBuildReport,
	FieldTektonStatus,
}

// ValidColumn reports if the column name is valid (part of the table columns).
func ValidColumn(column string) bool {
	for i := range Columns {
		if column == Columns[i] {
			return true
		}
	}
	return false
}

var (
	// CreatedByValidator is a validator for the "created_by" field. It is called by the builders before save.
	CreatedByValidator func(string) error
	// DefaultCreatedAt holds the default value on creation for the "created_at" field.
	DefaultCreatedAt func() time.Time
	// DefaultUpdatedAt holds the default value on creation for the "updated_at" field.
	DefaultUpdatedAt func() time.Time
	// UpdateDefaultUpdatedAt holds the default value on update for the "updated_at" field.
	UpdateDefaultUpdatedAt func() time.Time
	// ProductValidator is a validator for the "product" field. It is called by the builders before save.
	ProductValidator func(string) error
	// EditionValidator is a validator for the "edition" field. It is called by the builders before save.
	EditionValidator func(string) error
	// VersionValidator is a validator for the "version" field. It is called by the builders before save.
	VersionValidator func(string) error
	// GithubRepoValidator is a validator for the "github_repo" field. It is called by the builders before save.
	GithubRepoValidator func(string) error
	// GitRefValidator is a validator for the "git_ref" field. It is called by the builders before save.
	GitRefValidator func(string) error
	// GitHashValidator is a validator for the "git_hash" field. It is called by the builders before save.
	GitHashValidator func(string) error
	// PluginGitRefValidator is a validator for the "plugin_git_ref" field. It is called by the builders before save.
	PluginGitRefValidator func(string) error
	// DefaultIsHotfix holds the default value on creation for the "is_hotfix" field.
	DefaultIsHotfix bool
	// TargetImgValidator is a validator for the "target_img" field. It is called by the builders before save.
	TargetImgValidator func(string) error
	// DefaultPipelineEngine holds the default value on creation for the "pipeline_engine" field.
	DefaultPipelineEngine string
	// PipelineEngineValidator is a validator for the "pipeline_engine" field. It is called by the builders before save.
	PipelineEngineValidator func(string) error
	// BuilderImgValidator is a validator for the "builder_img" field. It is called by the builders before save.
	BuilderImgValidator func(string) error
	// BuildEnvValidator is a validator for the "build_env" field. It is called by the builders before save.
	BuildEnvValidator func(string) error
	// FeaturesValidator is a validator for the "features" field. It is called by the builders before save.
	FeaturesValidator func(string) error
	// ProductBaseImgValidator is a validator for the "product_base_img" field. It is called by the builders before save.
	ProductBaseImgValidator func(string) error
	// ProductDockerfileValidator is a validator for the "product_dockerfile" field. It is called by the builders before save.
	ProductDockerfileValidator func(string) error
	// DefaultStatus holds the default value on creation for the "status" field.
	DefaultStatus string
	// StatusValidator is a validator for the "status" field. It is called by the builders before save.
	StatusValidator func(string) error
	// ErrMsgValidator is a validator for the "err_msg" field. It is called by the builders before save.
	ErrMsgValidator func(string) error
)

// OrderOption defines the ordering options for the DevBuild queries.
type OrderOption func(*sql.Selector)

// ByID orders the results by the id field.
func ByID(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldID, opts...).ToFunc()
}

// ByCreatedBy orders the results by the created_by field.
func ByCreatedBy(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldCreatedBy, opts...).ToFunc()
}

// ByCreatedAt orders the results by the created_at field.
func ByCreatedAt(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldCreatedAt, opts...).ToFunc()
}

// ByUpdatedAt orders the results by the updated_at field.
func ByUpdatedAt(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldUpdatedAt, opts...).ToFunc()
}

// ByProduct orders the results by the product field.
func ByProduct(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldProduct, opts...).ToFunc()
}

// ByEdition orders the results by the edition field.
func ByEdition(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldEdition, opts...).ToFunc()
}

// ByVersion orders the results by the version field.
func ByVersion(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldVersion, opts...).ToFunc()
}

// ByGithubRepo orders the results by the github_repo field.
func ByGithubRepo(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldGithubRepo, opts...).ToFunc()
}

// ByGitRef orders the results by the git_ref field.
func ByGitRef(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldGitRef, opts...).ToFunc()
}

// ByGitHash orders the results by the git_hash field.
func ByGitHash(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldGitHash, opts...).ToFunc()
}

// ByPluginGitRef orders the results by the plugin_git_ref field.
func ByPluginGitRef(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldPluginGitRef, opts...).ToFunc()
}

// ByIsHotfix orders the results by the is_hotfix field.
func ByIsHotfix(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldIsHotfix, opts...).ToFunc()
}

// ByIsPushGcr orders the results by the is_push_gcr field.
func ByIsPushGcr(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldIsPushGcr, opts...).ToFunc()
}

// ByTargetImg orders the results by the target_img field.
func ByTargetImg(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldTargetImg, opts...).ToFunc()
}

// ByPipelineEngine orders the results by the pipeline_engine field.
func ByPipelineEngine(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldPipelineEngine, opts...).ToFunc()
}

// ByBuilderImg orders the results by the builder_img field.
func ByBuilderImg(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldBuilderImg, opts...).ToFunc()
}

// ByBuildEnv orders the results by the build_env field.
func ByBuildEnv(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldBuildEnv, opts...).ToFunc()
}

// ByFeatures orders the results by the features field.
func ByFeatures(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldFeatures, opts...).ToFunc()
}

// ByProductBaseImg orders the results by the product_base_img field.
func ByProductBaseImg(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldProductBaseImg, opts...).ToFunc()
}

// ByProductDockerfile orders the results by the product_dockerfile field.
func ByProductDockerfile(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldProductDockerfile, opts...).ToFunc()
}

// ByStatus orders the results by the status field.
func ByStatus(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldStatus, opts...).ToFunc()
}

// ByErrMsg orders the results by the err_msg field.
func ByErrMsg(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldErrMsg, opts...).ToFunc()
}

// ByPipelineBuildID orders the results by the pipeline_build_id field.
func ByPipelineBuildID(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldPipelineBuildID, opts...).ToFunc()
}

// ByPipelineStartAt orders the results by the pipeline_start_at field.
func ByPipelineStartAt(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldPipelineStartAt, opts...).ToFunc()
}

// ByPipelineEndAt orders the results by the pipeline_end_at field.
func ByPipelineEndAt(opts ...sql.OrderTermOption) OrderOption {
	return sql.OrderByField(FieldPipelineEndAt, opts...).ToFunc()
}
