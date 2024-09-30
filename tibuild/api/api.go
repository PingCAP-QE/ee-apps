package api

import (
	"context"
	"net/http"

	"github.com/gin-contrib/requestid"
	"github.com/gin-contrib/static"
	"github.com/gin-gonic/gin"
	swaggerfiles "github.com/swaggo/files"
	ginSwagger "github.com/swaggo/gin-swagger"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/controller"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/configs"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/database"
	controllers "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/controller"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
	events "github.com/PingCAP-QE/ee-apps/tibuild/pkg/webhook/handler"

	_ "github.com/PingCAP-QE/ee-apps/tibuild/docs"
)

// Create gin-routers
func Routers(file string, cfg *configs.ConfigYaml) (router *gin.Engine) {
	router = gin.Default()

	// Error
	routeError(router)

	// Cors
	routeCors(router)

	// Root html & Folder static/ for JS/CSS & home/*any for website('url' match to website/src/routes.js)
	routeHtml(router, file)

	// Real REST-API registry
	routeRestAPI(router, cfg)

	routeDocs(router)

	return router
}

func routeError(router *gin.Engine) {
	router.Use(APIErrorJSONReporter())
}

func routeCors(router *gin.Engine) {
	router.Use(Cors())
}

func routeHtml(router *gin.Engine, file string) {
	router.Use(
		static.Serve("/", static.LocalFile(file, true)),
	)
	router.Use(
		static.Serve("/static", static.LocalFile(file, true)),
	)
	homePages := router.Group("/home")
	{
		homePages.GET("/*any", func(c *gin.Context) {
			c.FileFromFS("/", http.Dir(file))
		})
	}
}

func routeDocs(router *gin.Engine) {
	router.GET("/swagger/*any", ginSwagger.WrapHandler(swaggerfiles.Handler))
}

func routeRestAPI(router *gin.Engine, cfg *configs.ConfigYaml) {

	build := router.Group("/build")
	{
		// 获取类-每个构建类型下的流水线
		build.GET("/pipelines-for-build-type", controller.PipelineForBuildType)
		// 获取类-每条流水线的执行列表
		build.GET("/pipeline-list-show", controller.PipelinesShow)
		// 获取类-每条流水线的可选参数值
		build.GET("/params-available-for-pipeline", controller.ParamsAvailableForPipeline)
		// 获取类-执行结果记录轮询
		build.GET("/request-rotation", controller.RequestRotation)
		//获取类-执行结果记录查询
		build.GET("/request-result", controller.RequestResult)
		// 触发类-触发流水线构建
		build.POST("/pipeline-trigger", controller.PipelineTrigger)
		// github sso 认证
		build.GET("/token", controller.GithubSSOToken)

	}

	apiGroup := router.Group("/api")
	apiGroup.Use(requestid.New())
	hotfixGroup := apiGroup.Group("/hotfix")
	hotfixHandler := controllers.NewHotfixHandler(cfg.Github.Token)
	{
		hotfixGroup.POST("/create-branch", hotfixHandler.CreateBranch)
		hotfixGroup.POST("/create-tag", hotfixHandler.CreateTag)
	}

	jenkins, err := service.NewJenkins(context.Background(), "https://cd.pingcap.net/", cfg.Jenkins.UserName, cfg.Jenkins.PassWord)
	if err != nil {
		panic(err)
	}
	devBuildGroup := apiGroup.Group("/devbuilds")
	devBuildServer := controllers.NewDevBuildServer(jenkins, database.DBConn.DB, cfg)
	devBuildHandler := controllers.NewDevBuildHandler(devBuildServer, cfg.RestApiSecret)
	{
		devBuildGroup.POST("", devBuildHandler.Create)
		devBuildGroup.GET("", devBuildHandler.List)
		devBuildGroup.GET("/:id", devBuildHandler.Get)
		devBuildGroup.PUT("/:id", devBuildHandler.Update)
		devBuildGroup.POST("/:id/rerun", devBuildHandler.Rerun)
	}

	artifactHelper := controllers.NewArtifactHelperHandler(jenkins)
	artifactGroup := apiGroup.Group("/artifact")
	{
		artifactGroup.POST("/sync-image", artifactHelper.SyncImage)
	}

	event := events.NewHandler(devBuildServer)
	eventsGroup := apiGroup.Group("/event")
	{
		eventsGroup.POST("", event.Receive)
	}
}
