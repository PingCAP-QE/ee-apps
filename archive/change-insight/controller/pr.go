package controller

import (
	"net/http"

	"github.com/PingCAP-QE/ee-apps/change-insight/action/pr"

	"github.com/gin-gonic/gin"
)

func ScanPRInfo(c *gin.Context) {
	org := c.Query("org")
	repo := c.Query("repo")
	status := c.Query("status")
	prList := pr.ScanPR(org, repo, status)
	c.JSON(http.StatusOK, gin.H{
		"prList": prList,
	})
}
