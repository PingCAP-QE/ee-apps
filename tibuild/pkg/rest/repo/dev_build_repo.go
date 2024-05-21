package repo

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"gorm.io/gorm"

	. "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type DevBuildRepo struct {
	Db *gorm.DB
}

func (m DevBuildRepo) Create(ctx context.Context, req DevBuild) (resp *DevBuild, err error) {
	if err = intoDB(&req); err != nil {
		return nil, err
	}
	if err := m.Db.Create(&req).Error; err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrInternalError)
	}
	return &req, nil
}

func (m DevBuildRepo) Get(ctx context.Context, id int) (resp *DevBuild, err error) {
	entity := DevBuild{}
	result := m.Db.Take(&entity, id)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, fmt.Errorf("build %d not found%w", id, ErrNotFound)
		} else {
			return nil, fmt.Errorf("%s%w", result.Error.Error(), ErrInternalError)
		}
	}
	err = outofDB(&entity)
	return &entity, err
}
func (m DevBuildRepo) Update(ctx context.Context, id int, req DevBuild) (resp *DevBuild, err error) {
	if err = intoDB(&req); err != nil {
		return nil, err
	}
	result := m.Db.Save(&req)
	if result.Error != nil {
		return nil, fmt.Errorf("%s%w", result.Error.Error(), ErrInternalError)
	}
	if err = outofDB(&req); err != nil {
		return nil, err
	}
	return &req, nil
}

func (m DevBuildRepo) List(ctx context.Context, option DevBuildListOption) (resp []DevBuild, err error) {
	db := m.Db.Order("created_at DESC").Offset(int(option.Offset)).Limit(int(option.Size))
	if option.Hotfix != nil {
		db = db.Where(&DevBuild{Spec: DevBuildSpec{IsHotfix: *option.Hotfix}})
	}
	if option.CreatedBy != nil {
		db = db.Where(&DevBuild{Meta: DevBuildMeta{CreatedBy: *option.CreatedBy}})
	}

	result := []DevBuild{}
	if err := db.Find(&result).Error; err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrInternalError)
	}

	for i := range result {
		err = outofDB(&result[i])
		if err != nil {
			return nil, err
		}
	}

	return result, err
}

func intoDB(entity *DevBuild) (err error) {
	js, err := json.Marshal(entity.Status.BuildReport)
	if err != nil {
		return err
	}
	entity.Status.BuildReportJson = json.RawMessage(js)

	tektonjs, err := json.Marshal(entity.Status.TektonStatus)
	if err != nil {
		return err
	}
	entity.Status.TektonStatusJson = json.RawMessage(tektonjs)
	return nil
}

func outofDB(entity *DevBuild) (err error) {
	report, err := fromRawMessage(entity.Status.BuildReportJson, &BuildReport{})
	if err != nil {
		return
	}
	entity.Status.BuildReport, _ = report.(*BuildReport)
	entity.Status.BuildReportJson = nil
	tekton, err := fromRawMessage(entity.Status.TektonStatusJson, &TektonStatus{})
	if err != nil {
		return
	}
	entity.Status.TektonStatus, _ = tekton.(*TektonStatus)
	entity.Status.TektonStatusJson = nil
	return
}

func fromRawMessage(js json.RawMessage, target any) (any, error) {
	bytes, _ := js.MarshalJSON()
	if string(bytes) == "null" {
		return nil, nil
	}
	err := json.Unmarshal(bytes, target)
	return target, err
}

var _ DevBuildRepository = DevBuildRepo{}
