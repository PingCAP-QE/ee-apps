package controller

import (
	"log"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/PingCAP-QE/ee-apps/change-insight/action/cm"
)

func PingAction(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"message": "pong",
	})
}

func GetConfigValue(c *gin.Context) {
	configeMap := cm.GetConfigValue()
	c.JSON(http.StatusOK, gin.H{
		"configureMap": configeMap,
	})
}

func GetConfigDataforVendor(c *gin.Context) {
	beginDate := c.Query("beginDate")
	endDate := c.Query("endDate")

	log.Println("beginDate : ", beginDate)
	log.Println("endDate : ", endDate)

	//configData := cm.CMInfoByDurationTime("2022.06.12", "2022.06.16")
	configData := cm.CMInfoByDurationTime(beginDate, endDate)
	c.JSON(http.StatusOK, gin.H{
		"configData": configData,
	})
}

func GetConfigDataforBranch(c *gin.Context) {
	branch1 := c.Query("branch1")
	branch2 := c.Query("branch2")
	configData := cm.CMInfoByBranch(branch1, branch2)
	c.JSON(http.StatusOK, gin.H{
		"configData": configData,
	})
}
