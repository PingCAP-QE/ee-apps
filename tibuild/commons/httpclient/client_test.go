package httpclient

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestGet(t *testing.T) {
	d, err := Get("https://www.baidu.com")
	assert.Equal(t, true, err == nil)
	assert.Equal(t, true, len(d) > 0)
}
