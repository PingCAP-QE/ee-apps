// Tool Url: https://github.com/go-gorm/gorm
// Tool Guide: https://gorm.io/docs/

package database

import (
	"log"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/configs"
)

// Mysql handler infomation
type MysqlInfo struct {
	DB *gorm.DB
	// Anything else...
}

var DBConn = &MysqlInfo{}

func Connect(config *configs.ConfigYaml) {
	conn, err := gorm.Open(mysql.Open(config.MysqlDSN), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Info),
	})
	if err != nil {
		log.Panicln(err)
	}

	sqlDB, err := conn.DB()
	if err != nil {
		log.Panicln(err)
	}
	sqlDB.SetMaxIdleConns(20)
	sqlDB.SetMaxOpenConns(100)
	sqlDB.SetConnMaxLifetime(time.Second * 600)

	DBConn.DB = conn
}
