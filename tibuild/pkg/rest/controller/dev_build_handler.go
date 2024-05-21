package controller

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"github.com/PingCAP-QE/ee-apps/tibuild/commons/configs"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/repo"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type DevBuildHandler struct {
	svc  service.DevBuildService
	auth configs.RestApiSecret
}

func NewDevBuildHandler(svc service.DevBuildService, auth configs.RestApiSecret) *DevBuildHandler {
	return &DevBuildHandler{
		svc:  svc,
		auth: auth,
	}
}

func NewDevBuildServer(jenkins service.Jenkins, db *gorm.DB, cfg *configs.ConfigYaml) service.DevBuildService {
	db.AutoMigrate(&service.DevBuild{})
	return &service.DevbuildServer{
		Repo:             repo.DevBuildRepo{Db: db},
		Jenkins:          jenkins,
		Now:              time.Now,
		Tekton:           service.NewCEClient(cfg.CloudEvent.Endpoint),
		GHClient:         service.NewGHClient(cfg.Github.Token),
		TektonViewURL:    cfg.TektonViewURL,
		OciFileserverURL: cfg.OciFileserverURL,
	}
}

func (h DevBuildHandler) authenticate(c *gin.Context) (context.Context, error) {
	user, passwd, ok := c.Request.BasicAuth()
	if !ok {
		return c.Request.Context(), nil
	}
	if user == service.AdminApiAccount {
		if passwd == h.auth.AdminToken {
			ctx := context.WithValue(c.Request.Context(), service.KeyOfApiAccount, user)
			return ctx, nil
		} else {
			return nil, fmt.Errorf("authenticate error%w", service.ErrAuth)
		}
	}
	if user == service.TibuildApiAccount {
		if passwd == h.auth.TiBuildToken {
			ctx := context.WithValue(c.Request.Context(), service.KeyOfApiAccount, user)
			return ctx, nil
		} else {
			return nil, fmt.Errorf("authenticate error%w", service.ErrAuth)
		}
	}
	return c.Request.Context(), nil
}

// CreateDevbuild godoc
//
//	@Summary		create and trigger devbuild
//	@Description	create and trigger devbuild
//	@Tags			devbuild
//	@Accept			json
//	@Produce		json
//	@Param			DevBuild	body		service.DevBuild	true	"build to create, only spec filed is required, others are ignored"
//	@Param			dryrun		query		bool				false	"dry run"	default(false)
//	@Success		200			{object}	service.DevBuild
//	@Failure		400			{object}	HTTPError
//	@Failure		500			{object}	HTTPError
//	@Router			/api/devbuilds [post]
func (h DevBuildHandler) Create(c *gin.Context) {
	ctx, err := h.authenticate(c)
	if err != nil {
		respondError(c, err)
		return
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
	entity, err := h.svc.Create(ctx, params, query)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entity)
}

// ListDevbuild godoc
//
//	@Summary		list devbuild
//	@Description	list devbuild
//	@Tags			devbuild
//	@Produce		json
//	@Param			size		query		int		false	"the size limit of items"		default(10)
//	@Param			offset		query		int		false	"the start position of items"	default(0)
//	@Param			hotfix		query		bool	false	"filter hotfix"					default(null)
//	@Param			createdBy	query		string	false	"filter created by"
//	@Success		200			{array}		service.DevBuild
//	@Failure		400			{object}	HTTPError
//	@Router			/api/devbuilds [get]
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
//
//	@Summary		get devbuild
//	@Description	get devbuild
//	@Tags			devbuild
//	@Produce		json
//	@Param			id		path		int		true	"id of build"
//	@Param			sync	query		bool	false	"whether sync with jenkins"	default(false)
//	@Success		200		{object}	service.DevBuild
//	@Failure		400		{object}	HTTPError
//	@Failure		500		{object}	HTTPError
//	@Router			/api/devbuilds/{id} [get]
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
//
//	@Summary		rerun devbuild
//	@Description	rerun devbuild
//	@Tags			devbuild
//	@Produce		json
//	@Param			id		path		int		true	"id of build"
//	@Param			dryrun	query		bool	false	"dry run"	default(false)
//	@Success		200		{object}	service.DevBuild
//	@Failure		400		{object}	HTTPError
//	@Failure		500		{object}	HTTPError
//	@Router			/api/devbuilds/{id}/rerun [post]
func (h DevBuildHandler) Rerun(c *gin.Context) {
	ctx, err := h.authenticate(c)
	if err != nil {
		respondError(c, err)
		return
	}
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
	entity, err := h.svc.Rerun(ctx, id, params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entity)
}

// UpdateDevbuild godoc
//
//	@Summary		update devbuild status
//	@Description	update the status field of a build
//	@Tags			devbuild
//	@Accept			json
//	@Produce		json
//	@Param			id			path		int					true	"id of build"
//	@Param			DevBuild	body		service.DevBuild	true	"build to update"
//	@Param			dryrun		query		bool				false	"dry run"	default(false)
//	@Success		200			{object}	service.DevBuild
//	@Failure		400			{object}	HTTPError
//	@Failure		500			{object}	HTTPError
//	@Router			/api/devbuilds/{id} [put]
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
