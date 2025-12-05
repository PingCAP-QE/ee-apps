package tiup

import "github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"

const redisKeyPrefixTiupRateLimit = "ratelimit:tiup"
const tiupServiceDeliveryCfgKey = "delivery_config_file"

type PublishRequestTiUP struct {
	From       share.From      `json:"from,omitzero"`
	Publish    PublishInfoTiUP `json:"publish,omitzero"`
	TiupMirror string          `json:"tiup_mirror,omitempty"`
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

type DeliveryRule struct {
	Description     string   `json:"description,omitempty"`
	TagsRegex       []string `json:"tags_regex"`
	DestMirrors     []string `json:"dest_mirrors"`
	Nightly         bool     `json:"nightly,omitempty"`
	TagRegexReplace *string  `json:"tag_regex_replace,omitempty"`
}

type DeliveryConfig struct {
	TiupPublishRules map[string][]DeliveryRule `json:"tiup_publish_rules,omitempty"`
}
