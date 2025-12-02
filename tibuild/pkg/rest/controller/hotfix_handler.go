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
//	@Summary		create hotfix branch
//	@Description	create hotfix branch
//	@Tags			hotfix
//	@Accept			json
//	@Produce		json
//	@Param			BranchCreateReq	body		service.BranchCreateReq	true	"hotfix param"
//	@Success		200				{object}	service.BranchCreateResp
//	@Failure		422				{object}	HTTPError
//	@Failure		400				{object}	HTTPError
//	@Failure		500				{object}	HTTPError
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
//	@Summary		create hotfix tag
//	@Description	create hotfix tag
//	@Tags			hotfix
//	@Accept			json
//	@Produce		json
//	@Param			TagCreateReq	body		service.TagCreateReq	true	"hotfix param"
//	@Success		200				{object}	service.TagCreateResp
//	@Failure		422				{object}	HTTPError
//	@Failure		400				{object}	HTTPError
//	@Failure		500				{object}	HTTPError
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

// // CreateTidbXHotfixTag godoc
//	@Summary		create tidb-x hotfix tag
//	@Description	create tidb-x hotfix git tag with auto-incremented tag name (vX.Y.Z-nextgen.YYYYMM.N)
//	@Tags			hotfix
//	@Accept			json
//	@Produce		json
//	@Param			TidbXHotfixTagCreateReq	body		service.TidbXHotfixTagCreateReq	true	"tidb-x hotfix tag param"
//	@Success		200						{object}	service.TidbXHotfixTagCreateResp
//	@Failure		422						{object}	HTTPError
//	@Failure		400						{object}	HTTPError
//	@Failure		500						{object}	HTTPError
// // @Router /api/hotfix/create-tidb-x-tag [post]
func (h HotfixHandler) CreateTidbXHotfixTag(c *gin.Context) {
	params := service.TidbXHotfixTagCreateReq{}
	err := bindParam(&params, c)
	if err != nil {
		return
	}
	resp, err := h.svc.CreateTidbXHotfixTag(c.Request.Context(), params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, resp)
}

func NewHotfixHandler(token string) HotfixHandler {
	return HotfixHandler{svc: service.NewRepoService(repo.NewGithubHeadCreator(token))}
}
