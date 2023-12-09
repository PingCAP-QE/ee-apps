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
	result := []DevBuild{}
	db := m.Db.Order("created_at DESC")
	if option.Hotfix != nil {
		db = db.Where(&DevBuild{Spec: DevBuildSpec{IsHotfix: *option.Hotfix}}, "IsHotfix")
	}
	if err := db.Offset(int(option.Offset)).Limit(int(option.Size)).Find(&result).Error; err != nil {
		return nil, fmt.Errorf("%s%w", err.Error(), ErrInternalError)
	}
	return result, err
}

func intoDB(entity *DevBuild) (err error) {
	js, err := json.Marshal(entity.Status.BuildReport)
	if err != nil {
		return err
	}
	entity.Status.BuildReportJson = json.RawMessage(js)
	return nil
}

func outofDB(entity *DevBuild) (err error) {
	entity.Status.BuildReport, err = fromRawMessage(entity.Status.BuildReportJson)
	return
}

func fromRawMessage(js json.RawMessage) (*BuildReport, error) {
	bytes, _ := js.MarshalJSON()
	if string(bytes) == "null" {
		return nil, nil
	}
	report := BuildReport{}
	err := json.Unmarshal(bytes, &report)
	if err != nil {
		return nil, err
	}
	return &report, nil
}

var _ DevBuildRepository = DevBuildRepo{}
