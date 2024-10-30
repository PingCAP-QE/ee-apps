package impl

import "time"

const (
	EventTypeTiupPublishRequest = "net.pingcap.tibuild.tiup-publish-request"
	EventTypeFsPublishRequest   = "net.pingcap.tibuild.fs-publish-request"

	FromTypeOci  = "oci"
	FromTypeHTTP = "http"

	PublishStateQueued     = "queued"
	PublishStateProcessing = "processing"
	PublishStateSuccess    = "success"
	PublishStateFailed     = "failed"
	PublishStateCanceled   = "canceled"

	DefaultStateTTL            = 12 * time.Hour
	DefaultTiupNightlyInternal = 12 * time.Hour
)

type PublishRequest struct {
	From    From        `json:"from,omitempty"`
	Publish PublishInfo `json:"publish,omitempty"`
}

type From struct {
	Type string    `json:"type,omitempty"`
	Oci  *FromOci  `json:"oci,omitempty"`
	HTTP *FromHTTP `json:"http,omitempty"`
}

type PublishInfo struct {
	Name        string `json:"name,omitempty"` // tiup pkg name or component name for fileserver
	OS          string `json:"os,omitempty"`
	Arch        string `json:"arch,omitempty"`
	Version     string `json:"version,omitempty"`
	Description string `json:"description,omitempty"` // ignore for `EventTypeFsPublishRequest`
	EntryPoint  string `json:"entry_point,omitempty"` // ignore for `EventTypeFsPublishRequest`
	Standalone  bool   `json:"standalone,omitempty"`  // ignore for `EventTypeFsPublishRequest`
}

func (p *PublishInfo) IsNightlyTiup() bool {
	return tiupVersionRegex.MatchString(p.Version)
}

type FromOci struct {
	Repo string `json:"repo,omitempty"`
	Tag  string `json:"tag,omitempty"`
	File string `json:"file,omitempty"`
}

type FromHTTP struct {
	URL string `json:"url,omitempty"`
}
