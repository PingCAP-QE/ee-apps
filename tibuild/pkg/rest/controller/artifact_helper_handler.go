package controller

import (
	"net/http"
	"tibuild/pkg/rest/service"

	"github.com/gin-gonic/gin"
)

type ArtifactHelperHandler struct {
	svc service.ArtifactHelperService
}

func NewArtifactHelperHandler(j service.Jenkins) *ArtifactHelperHandler {
	return &ArtifactHelperHandler{svc: service.NewArtifactHelper(j)}
}

// SyncImage godoc
// @Summary	sync hotfix image to dockerhub
// @Description	sync
// @Tags	artifact
// @Accept json
// @Produce json
// @Param	ImageSyncRequest	body	service.ImageSyncRequest	true	"image sync to public, only hotfix is accepted right now"
// @Success	200 {object}	service.ImageSyncRequest
// @Failure	400	{object}	HTTPError
// @Failure	500	{object}	HTTPError
// @Router /api/artifact/sync-image [post]
func (h ArtifactHelperHandler) SyncImage(c *gin.Context) {
	params := service.ImageSyncRequest{}
	err := bindParam(&params, c)
	if err != nil {
		return
	}
	entity, err := h.svc.SyncImage(c.Request.Context(), params)
	if err != nil {
		respondError(c, err)
		return
	}
	c.JSON(http.StatusOK, entity)
}
