package main

import (
	"github.com/PingCAP-QE/ee-apps/tibuild/api"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/configs"
)

func main() {
	// Load config
	configs.LoadConfig("configs/config.yaml")

	// Connect database
	database.Connect(configs.Config)

	//controller.SetAutoIncrementOffset()

	// Start website && REST-API
	router := api.Routers("./website/build/", configs.Config)
	router.Run(":8080")
}
