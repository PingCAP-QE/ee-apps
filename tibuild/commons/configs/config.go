// Tool Url: https://github.com/jinzhu/configor

package configs

import (
	"fmt"

	"github.com/jinzhu/configor"
)

// Database configuration
type ConfigYaml struct {
	Mysql struct {
		UserName string `default:"tibuild"`
		PassWord string `required:"true"`
		Host     string `required:"true"`
		Port     string `default:"3306"`
		DataBase string `required:"true"`
		CharSet  string `default:"utf8"`
		TimeZone string `default:"Asia%2FShanghai"`
	}

	Jenkins struct {
		UserName string `required:"true"`
		PassWord string `required:"true"`
	}

	StaticDir string `default:"/goapp/website/build/"`

	Github struct {
		Token string
	}

	TiBuild struct {
		AdminPasswd string
	}
}

var Config = &ConfigYaml{}

// Load config from file into 'Config' variable
func LoadConfig(file string) {
	fmt.Printf("file:%s\n", file)
	configor.Load(Config, file)
}
