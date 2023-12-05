package main

import (
	"tibuild/api"
	"tibuild/commons/configs"
	"tibuild/commons/database"
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
