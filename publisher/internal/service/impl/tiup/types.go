package tiup

import "github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"

const redisKeyPrefixTiupRateLimit = "ratelimit:tiup"

type PublishRequestTiUP struct {
	From    share.From      `json:"from,omitzero"`
	Publish PublishInfoTiUP `json:"publish,omitzero"`
}

type PublishInfoTiUP struct {
	Name        string `json:"name,omitempty"`        // tiup pkg name or component name for fileserver
	OS          string `json:"os,omitempty"`          // ignore for `EventTypeFsPublishRequest`
	Arch        string `json:"arch,omitempty"`        // ignore for `EventTypeFsPublishRequest`
	Version     string `json:"version,omitempty"`     // SemVer format for `EventTypeTiupPublishRequest` and "<git-branch>#<git-commit-sha1>" for `EventTypeFsPublishRequest`
	Description string `json:"description,omitempty"` // ignore for `EventTypeFsPublishRequest`
	EntryPoint  string `json:"entry_point,omitempty"` // if event is `EventTypeFsPublishRequest`, the the value is the basename for store file, like tidb-server.tar.gz
	Standalone  bool   `json:"standalone,omitempty"`  // ignore for `EventTypeFsPublishRequest`
}
