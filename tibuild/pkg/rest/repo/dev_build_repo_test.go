package repo

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/stretchr/testify/require"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"

	. "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

func TestDevBuildCreate(t *testing.T) {
	rdb, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer rdb.Close()
	odb, err := gorm.Open(mysql.New(mysql.Config{Conn: rdb, SkipInitializeWithVersion: true}))
	require.NoError(t, err)
	repo := DevBuildRepo{Db: odb}
	mock.ExpectBegin()
	now := time.Unix(1, 0)
	mock.ExpectExec("INSERT INTO `dev_builds`").WithArgs(now, "", now, ProductBr, "", "", "v6.7.0", EditionCommunity, "", "",
		"AA=BB", "https://raw.example.com/Dockerfile", "", "pingcap/builder", "", false, "", false, "", /* target_img */
		JenkinsEngine, "PENDING", 0, "", nil, nil,
		json.RawMessage("null"), json.RawMessage("null")).WillReturnResult(sqlmock.NewResult(1, 1))

	mock.ExpectCommit()
	entity, err := repo.Create(context.TODO(), DevBuild{
		Meta: DevBuildMeta{CreatedAt: now, UpdatedAt: now},
		Spec: DevBuildSpec{Product: ProductBr, Version: "v6.7.0", Edition: EditionCommunity,
			BuildEnv: "AA=BB", ProductDockerfile: "https://raw.example.com/Dockerfile",
			BuilderImg:     "pingcap/builder",
			PipelineEngine: JenkinsEngine},
		Status: DevBuildStatus{Status: BuildStatusPending}})
	require.NoError(t, err)
	require.Equal(t, 1, entity.ID)
}

func TestDevBuildList(t *testing.T) {
	rdb, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer rdb.Close()
	odb, err := gorm.Open(mysql.New(mysql.Config{Conn: rdb, SkipInitializeWithVersion: true}))
	require.NoError(t, err)
	repo := DevBuildRepo{Db: odb}
	rows := sqlmock.NewRows([]string{"id"}).AddRow(1).AddRow(2)
	mock.ExpectQuery("SELECT \\* FROM `dev_builds`  WHERE  `dev_builds`.`is_hotfix` = \\? ORDER BY created_at DESC LIMIT \\? OFFSET \\?").
		WithArgs(false, 10, 5).WillReturnRows(rows)
	entities, err := repo.List(context.TODO(), DevBuildListOption{Offset: 5, Size: 10, Hotfix: &[]bool{false}[0]})
	require.NoError(t, err)
	require.Equal(t, 2, len(entities))
}

func TestDevBuildUpdate(t *testing.T) {
	rdb, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer rdb.Close()
	odb, err := gorm.Open(mysql.New(mysql.Config{Conn: rdb, SkipInitializeWithVersion: true}))
	require.NoError(t, err)
	repo := DevBuildRepo{Db: odb}
	now := time.Unix(1, 0)
	report := BuildReport{GitHash: "a1b2c3"}
	report_text, err := json.Marshal(report)
	tekton_status := TektonStatus{}
	tekton_text, err := json.Marshal(tekton_status)
	require.NoError(t, err)
	mock.ExpectBegin()
	mock.ExpectExec("UPDATE `dev_builds` SET").WithArgs(now, "", sqlmock.AnyArg(), ProductBr, "", "", "", "", "", "", "", "", "", "", "", false, "", false, "", "", "SUCCESS", 0, "", nil, nil, report_text, tekton_text, 1).WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()
	entity, err := repo.Update(context.TODO(),
		1,
		DevBuild{ID: 1,
			Meta:   DevBuildMeta{CreatedAt: now, UpdatedAt: now},
			Spec:   DevBuildSpec{Product: ProductBr},
			Status: DevBuildStatus{Status: BuildStatusSuccess, BuildReport: &report, TektonStatus: &tekton_status},
		})
	require.NoError(t, err)
	require.Equal(t, BuildStatusSuccess, entity.Status.Status)
}

func TestDevBuildGet(t *testing.T) {
	rdb, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer rdb.Close()
	odb, err := gorm.Open(mysql.New(mysql.Config{Conn: rdb, SkipInitializeWithVersion: true}))
	require.NoError(t, err)
	repo := DevBuildRepo{Db: odb}
	t.Run("proccessing", func(t *testing.T) {
		rows := sqlmock.NewRows([]string{"id", "status"}).AddRow(1, BuildStatusProcessing)
		mock.ExpectQuery("SELECT \\* FROM `dev_builds` WHERE `dev_builds`.`id` = \\? LIMIT \\?").WillReturnRows(rows)
		entity, err := repo.Get(context.TODO(), 1)
		require.NoError(t, err)
		require.Nil(t, entity.Status.BuildReport)
		require.Equal(t, BuildStatusProcessing, entity.Status.Status)
	})
	t.Run("report", func(t *testing.T) {
		report := BuildReport{GitHash: "a1b2"}
		report_text, err := json.Marshal(report)
		require.NoError(t, err)
		rows := sqlmock.NewRows([]string{"id", "status", "build_report"}).AddRow(1, BuildStatusSuccess, report_text)
		mock.ExpectQuery("SELECT \\* FROM `dev_builds` WHERE `dev_builds`.`id` = \\? LIMIT \\?").WillReturnRows(rows)
		entity, err := repo.Get(context.TODO(), 1)
		require.NoError(t, err)
		require.Equal(t, report, *entity.Status.BuildReport)
		require.Empty(t, entity.Status.BuildReportJson)
	})
	t.Run("tekton_status", func(t *testing.T) {
		tekton := TektonStatus{}
		tekton_text, err := json.Marshal(tekton)
		require.NoError(t, err)
		rows := sqlmock.NewRows([]string{"id", "status", "tekton_status"}).AddRow(1, BuildStatusSuccess, tekton_text)
		mock.ExpectQuery("SELECT \\* FROM `dev_builds` WHERE `dev_builds`.`id` = \\? LIMIT \\?").WillReturnRows(rows)
		entity, err := repo.Get(context.TODO(), 1)
		require.NoError(t, err)
		require.Equal(t, tekton, *entity.Status.TektonStatus)
		require.Empty(t, entity.Status.TektonStatusJson)
	})
	t.Run("not found", func(t *testing.T) {
		mock.ExpectQuery("SELECT \\* FROM `dev_builds` WHERE `dev_builds`.`id` = \\? LIMIT \\?").WillReturnError(gorm.ErrRecordNotFound)
		entity, err := repo.Get(context.TODO(), 1)
		require.Nil(t, entity)
		require.ErrorIs(t, err, ErrNotFound)
	})
}

func TestDevbuildOutofDb(t *testing.T) {
	entity := &DevBuild{
		Status: DevBuildStatus{
			BuildReportJson: json.RawMessage("null"),
		},
	}
	err := outofDB(entity)
	require.NoError(t, err)
}
