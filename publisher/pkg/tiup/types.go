package tiup

const (
	EventTypeTiupPublishRequest = "net.pingcap.tibuild.tiup-publish-request"

	FromTypeOci  = "oci"
	FromTypeHTTP = "http"
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
	Name        string `json:"name,omitempty"`
	OS          string `json:"os,omitempty"`
	Arch        string `json:"arch,omitempty"`
	Version     string `json:"version,omitempty"`
	Description string `json:"description,omitempty"`
	EntryPoint  string `json:"entry_point,omitempty"`
	Standalone  bool   `json:"standalone,omitempty"`
}

type FromOci struct {
	Repo string `json:"repo,omitempty"`
	Tag  string `json:"tag,omitempty"`
	File string `json:"file,omitempty"`
}

type FromHTTP struct {
	URL string `json:"url,omitempty"`
}
