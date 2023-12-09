package controller

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/repo"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type DevBuildHandler struct {
	svc service.DevBuildService
}

func NewDevBuildHandler(ctx context.Context, jenkins service.Jenkins, db *gorm.DB) *DevBuildHandler {
	db.AutoMigrate(&service.DevBuild{})
	return &DevBuildHandler{svc: service.DevbuildServer{
		Repo:    repo.DevBuildRepo{Db: db},
		Jenkins: jenkins,
		Now:     time.Now},
	}
}

// CreateDevbuild godoc
// @Summary	create and trigger devbuild
// @Description	create and trigger devbuild
// @Tags	devbuild
// @Accept json
// @Produce json
// @Param	DevBuild	body	service.DevBuild	true	"build to create, only spec filed is required, others are ignored"
// @Param	dryrun	query	bool	false	"dry run"	default(false)
// @Success	200 {object}	service.DevBuild
// @Failure	400	{object}	HTTPError
// @Failure	500	{object}	HTTPError
// @Router /api/devbuilds [post]
func (h DevBuildHandler) Create(c *gin.Context) {
	params := service.DevBuild{}
	err := bindParam(&params, c)
	if err != nil {
		return
	}
	query := service.DevBuildSaveOption{}
	err = c.ShouldBindQuery(&query)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
		return
	}
	entity, err := h.svc.Create(c.Request.Context(), params, query)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entity)
}

// ListDevbuild godoc
// @Summary	list devbuild
// @Description	list devbuild
// @Tags	devbuild
// @Produce json
// @Param	size	query	int	false	"the size limit of items"	default(10)
// @Param	offset	query	int	false	"the start position of items"	default(0)
// @Param	hotfix	query	bool	false	"filter hotfix"	default(null)
// @Success	200 {array}	service.DevBuild
// @Failure	400	{object}	HTTPError
// @Router /api/devbuilds [get]
func (h DevBuildHandler) List(c *gin.Context) {
	params := service.DevBuildListOption{}
	err := c.ShouldBindQuery(&params)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
		return
	}
	if params.Size == 0 {
		params.Size = 10
	}
	entities, err := h.svc.List(c.Request.Context(), params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entities)
}

// GetDevbuild godoc
// @Summary	get devbuild
// @Description	get devbuild
// @Tags	devbuild
// @Produce json
// @Param	id	path	int	true	"id of build"
// @Param	sync	query	bool	false	"whether sync with jenkins"	default(false)
// @Success	200 {object}	service.DevBuild
// @Failure	400	{object}	HTTPError
// @Failure	500	{object}	HTTPError
// @Router /api/devbuilds/{id} [get]
func (h DevBuildHandler) Get(c *gin.Context) {
	idStr := c.Param("id")
	id, err := strconv.Atoi(idStr)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
		return
	}
	params := service.DevBuildGetOption{Sync: false}
	err = c.ShouldBindQuery(&params)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
		return
	}
	entity, err := h.svc.Get(c.Request.Context(), id, params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entity)
}

// GetDevbuild godoc
// @Summary	rerun devbuild
// @Description	rerun devbuild
// @Tags	devbuild
// @Produce json
// @Param	id	path	int	true	"id of build"
// @Param	dryrun	query	bool	false	"dry run"	default(false)
// @Success	200 {object}	service.DevBuild
// @Failure	400	{object}	HTTPError
// @Failure	500	{object}	HTTPError
// @Router /api/devbuilds/{id}/rerun [post]
func (h DevBuildHandler) Rerun(c *gin.Context) {
	idStr := c.Param("id")
	id, err := strconv.Atoi(idStr)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
		return
	}
	params := service.DevBuildSaveOption{}
	err = c.ShouldBindQuery(&params)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
		return
	}
	entity, err := h.svc.Rerun(c.Request.Context(), id, params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entity)
}

// UpdateDevbuild godoc
// @Summary	update devbuild status
// @Description	update the status field of a build
// @Tags	devbuild
// @Accept json
// @Produce json
// @Param	id	path	int	true	"id of build"
// @Param	DevBuild	body	service.DevBuild	true	"build to update"
// @Param	dryrun	query	bool	false	"dry run"	default(false)
// @Success	200 {object}	service.DevBuild
// @Failure	400	{object}	HTTPError
// @Failure	500	{object}	HTTPError
// @Router /api/devbuilds/{id} [put]
func (h DevBuildHandler) Update(c *gin.Context) {
	idStr := c.Param("id")
	id, err := strconv.Atoi(idStr)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
	}
	params := service.DevBuild{}
	err = bindParam(&params, c)
	if err != nil {
		return
	}
	query := service.DevBuildSaveOption{}
	err = c.ShouldBindQuery(&query)
	if err != nil {
		respondError(c, fmt.Errorf("%s%w", err.Error(), service.ErrBadRequest))
		return
	}
	entity, err := h.svc.Update(c.Request.Context(), id, params, query)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entity)
}
