// Tool Url: https://github.com/jinzhu/configor

package configs

import (
	"fmt"
	"time"

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
		Endpoint          string `yaml:"endpoint,omitempty" json:"endpoint,omitempty"`
		TektonDirectTrigger bool   `yaml:"tekton_direct_trigger,omitempty" json:"tekton_direct_trigger,omitempty"`
	} `yaml:"cloudevent,omitempty" json:"cloudevent,omitempty"`

	TektonViewURL    string `yaml:"tektonviewurl,omitempty" json:"tektonviewurl,omitempty"`
	OciFileserverURL string `yaml:"ocifileserverurl,omitempty" json:"ocifileserverurl,omitempty"`

	// ImageMirrorURLMap is a map prefixes for transformation between direct url to mirror url.
	ImageMirrorURLMap map[string]string `yaml:"image_mirror_url_map,omitempty" json:"image_mirror_url_map,omitempty"`

	// TektonReconciler configures the background reconciler for Tekton PipelineRun status.
	TektonReconciler struct {
		Enabled        bool          `yaml:"enabled" json:"enabled"`
		Namespace      string        `yaml:"namespace" json:"namespace"`
		Interval       time.Duration `yaml:"interval" json:"interval"`
		StaleThreshold time.Duration `yaml:"stale_threshold" json:"stale_threshold"`
	} `yaml:"tekton_reconciler,omitempty" json:"tekton_reconciler,omitempty"`

	// Lark configures Lark notifications for build completion events.
	Lark struct {
		Enabled    bool   `yaml:"enabled" json:"enabled"`
		WebhookURL string `yaml:"webhook_url,omitempty" json:"webhook_url,omitempty"`
	} `yaml:"lark,omitempty" json:"lark,omitempty"`
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
