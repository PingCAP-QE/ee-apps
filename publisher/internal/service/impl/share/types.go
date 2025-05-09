package share

import (
	"time"
)

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

type From struct {
	Type string    `json:"type,omitempty"`
	Oci  *FromOci  `json:"oci,omitempty"`
	HTTP *FromHTTP `json:"http,omitempty"`
}

func (f From) String() string {
	switch f.Type {
	case FromTypeOci:
		return f.Oci.String()
	case FromTypeHTTP:
		return f.HTTP.URL
	default:
		return ""
	}
}

type FromOci struct {
	Repo string `json:"repo,omitempty"`
	Tag  string `json:"tag,omitempty"`
	File string `json:"file,omitempty"`
}

func (f FromOci) String() string {
	return f.Repo + ":" + f.Tag + "#" + f.File
}

type FromHTTP struct {
	URL string `json:"url,omitempty"`
}
