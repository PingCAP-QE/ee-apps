package impl

import (
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
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

type PublishRequestTiUP struct {
	From    From            `json:"from,omitempty"`
	Publish PublishInfoTiUP `json:"publish,omitempty"`
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

type PublishRequestFS struct {
	From    From          `json:"from,omitempty"`
	Publish PublishInfoFS `json:"publish,omitempty"`
}

type PublishInfoFS struct {
	Repo            string            `json:"repo,omitempty"`
	Branch          string            `json:"branch,omitempty"`
	CommitSHA       string            `json:"commit_sha,omitempty"`
	FileTransferMap map[string]string `json:"file_transfer_map,omitempty"`
}

// Worker provides handling for cloud events.
type Worker interface {
	Handle(event cloudevents.Event) cloudevents.Result
}
