package main

import (
	"fmt"
	"github.com/grafana/grafana-plugin-sdk-go/backend"
	"github.com/justinas/nosurf"
	"log"
	"net/http"
	"path/filepath"
	"strings"

	"github.com/PingCAP-QE/ee-apps/change-insight/action/cm"
	"github.com/PingCAP-QE/ee-apps/change-insight/controller"

	"github.com/gin-gonic/gin"
	"github.com/spf13/pflag"
	"github.com/spf13/viper"
)

func initconfig(configFile string) {
	log.Println("[configFile]", configFile)
	paths, fileName := filepath.Split(configFile)
	log.Println("paths:", paths)
	log.Println("fileName:", fileName)
	fileNameNoExt := strings.TrimRight(fileName, filepath.Ext(configFile))
	fileNameType := strings.TrimLeft(filepath.Ext(configFile), ".")

	log.Println("fileNameNoExt:", fileNameNoExt)
	log.Println("fileNameType:", fileNameType)

	viper.SetConfigName(fileNameNoExt)
	viper.SetConfigType(fileNameType)
	viper.AddConfigPath(paths)

	if err := viper.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); ok {
			// Config file not found; ignore error if desired
			log.Println("no such config file")
		} else {
			// Config file was found but another error was produced
			log.Println("read config error")
		}
		log.Fatal(err) // failed to read configuration file. Fatal error
	}
}

func main() {

	pflag.String("conf", "./conf/conf_dev.yaml", "specfic the config file")

	pflag.Parse()
	if err := viper.BindPFlags(pflag.CommandLine); err != nil {
		log.Fatalln(err)
	}

	configFile := viper.GetString("conf")
	initconfig(configFile)

	port := viper.GetString("port")

	cm.WorkSpace = viper.GetString("workspace.path")

	fmt.Println(nosurf.CookieName)
	response := backend.DataResponse{
		Error: fmt.Errorf("example error"),
	}

	fmt.Println("Example response:", response)
	r := gin.Default()
	r.Static("/static/", "./static")
	r.LoadHTMLGlob("template/*")
	r.GET("/ping", controller.PingAction)
	r.GET("/conf_debug", controller.GetConfigValue)
	r.GET("/confiChangeDuringDate", controller.GetConfigDataforVendor)
	r.GET("/configChangeDuringRelease", controller.GetConfigDataforBranch)

	r.GET("/scanPR", controller.ScanPRInfo)
	r.GET("/index", func(ctx *gin.Context) {
		ctx.HTML(http.StatusOK, "index.html", gin.H{
			"title": "代码提交记录分析平台",
			"body":  "",
		})
	})
	if err := r.Run(":" + port); err != nil {
		log.Fatalln(err)
	}
}
