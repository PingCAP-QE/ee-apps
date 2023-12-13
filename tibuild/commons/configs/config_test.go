package configs

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestLoadConfig(t *testing.T) {
	LoadConfig("../../configs/config_example.yaml")
	cfg := Config.AuthConfig
	assert.NotEmpty(t, cfg.TiBuildPasswd)
}
