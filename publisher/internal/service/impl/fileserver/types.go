package fileserver

import "github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"

type PublishRequestFS struct {
	From    share.From    `json:"from,omitzero"`
	Publish PublishInfoFS `json:"publish,omitzero"`
}

type PublishInfoFS struct {
	Repo            string            `json:"repo,omitempty"`
	Branch          string            `json:"branch,omitempty"`
	CommitSHA       string            `json:"commit_sha,omitempty"`
	FileTransferMap map[string]string `json:"file_transfer_map,omitempty"`
}
