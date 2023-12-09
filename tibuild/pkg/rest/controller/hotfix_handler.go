package controller

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/repo"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type HotfixHandler struct {
	svc service.HotfixService
}

// // CreateHotfixBranch godoc
// @Summary	create hotfix branch
// @Description	create hotfix branch
// @Tags	hotfix
// @Accept json
// @Produce json
// @Param	BranchCreateReq	body	service.BranchCreateReq	true	"hotfix param"
// @Success	200 {object}	service.BranchCreateResp
// @Failure	422	{object}	HTTPError
// @Failure	400	{object}	HTTPError
// @Failure	500	{object}	HTTPError
// // @Router /api/hotfix/create-branch [post]
func (h HotfixHandler) CreateBranch(c *gin.Context) {
	params := service.BranchCreateReq{}
	err := bindParam(&params, c)
	if err != nil {
		return
	}
	branch, err := h.svc.CreateBranch(c.Request.Context(), params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, branch)
}

// // CreateHotfixTag godoc
// @Summary	create hotfix tag
// @Description	create hotfix tag
// @Tags	hotfix
// @Accept json
// @Produce json
// @Param	TagCreateReq	body	service.TagCreateReq	true	"hotfix param"
// @Success	200 {object}	service.TagCreateResp
// @Failure	422	{object}	HTTPError
// @Failure	400	{object}	HTTPError
// @Failure	500	{object}	HTTPError
// // @Router /api/hotfix/create-tag [post]
func (h HotfixHandler) CreateTag(c *gin.Context) {
	params := service.TagCreateReq{}
	err := bindParam(&params, c)
	if err != nil {
		return
	}
	branch, err := h.svc.CreateTag(c.Request.Context(), params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, branch)
}

func NewHotfixHandler(token string) HotfixHandler {
	return HotfixHandler{svc: service.NewRepoService(repo.NewGithubHeadCreator(token))}
}
