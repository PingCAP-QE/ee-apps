package repo

import (
	"testing"

	"github.com/stretchr/testify/assert"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

func TestGenEEChatOpsCreateBranch(t *testing.T) {
	actual := GenEEChatOpsCreateBranch(
		service.RepoTidb,
		"release-6.1-20220904-v6.1.1",
		"v6.1.1",
	)
	assert.Equal(t,
		` /create_branch_from_tag https://github.com/pingcap/tidb/releases/tag/v6.1.1 release-6.1-20220904-v6.1.1`,
		actual)
}

func TestGenEEChatOpsCreateTag(t *testing.T) {
	actual := GenEEChatOpsCreateTag(
		service.RepoTidb,
		"v6.1.1-20220905",
		"release-6.1-20220904-v6.1.1",
	)
	assert.Equal(t,
		` /create_tag_from_branch https://github.com/pingcap/tidb/tree/release-6.1-20220904-v6.1.1 v6.1.1-20220905`,
		actual)
}
