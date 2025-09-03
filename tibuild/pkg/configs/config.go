// Tool Url: https://github.com/jinzhu/configor

package configs

import (
	"fmt"

	"github.com/jinzhu/configor"
)

// Database configuration
type ConfigYaml struct {
	MysqlDSN string `yaml:"mysql_dsn,omitempty" json:"mysql_dsn,omitempty"`

	Jenkins struct {
		UserName string `required:"true" yaml:"username,omitempty" json:"username,omitempty"`
		PassWord string `required:"true" yaml:"password,omitempty" json:"password,omitempty"`
	} `yaml:"jenkins,omitempty" json:"jenkins,omitempty"`

	StaticDir string `default:"/goapp/website/build/" yaml:"static_dir,omitempty" json:"static_dir,omitempty"`

	Github struct {
		Token string `yaml:"token,omitempty" json:"token,omitempty"`
	} `yaml:"github,omitempty" json:"github,omitempty"`

	RestApiSecret RestApiSecret `yaml:"restapisecret,omitempty" json:"restapisecret,omitempty"`
	CloudEvent    struct {
		Endpoint string `yaml:"endpoint,omitempty" json:"endpoint,omitempty"`
	} `yaml:"cloudevent,omitempty" json:"cloudevent,omitempty"`

	TektonViewURL    string `yaml:"tektonviewurl,omitempty" json:"tektonviewurl,omitempty"`
	OciFileserverURL string `yaml:"ocifileserverurl,omitempty" json:"ocifileserverurl,omitempty"`

	// ImageMirrorURLMap is a map prefixes for transformation between direct url to mirror url.
	ImageMirrorURLMap map[string]string `yaml:"image_mirror_url_map,omitempty" json:"image_mirror_url_map,omitempty"`
}

type RestApiSecret struct {
	AdminToken   string `yaml:"admintoken,omitempty" json:"admintoken,omitempty"`
	TiBuildToken string `yaml:"tibuildtoken,omitempty" json:"tibuildtoken,omitempty"`
}

var Config = &ConfigYaml{}

// Load config from file into 'Config' variable
func LoadConfig(file string) {
	fmt.Printf("file:%s\n", file)
	err := configor.Load(Config, file)
	if err != nil {
		panic(err)
	}
}
