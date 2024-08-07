// Code generated by goa v3.16.1, DO NOT EDIT.
//
// ks3 HTTP client CLI support package
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package client

import (
	ks3 "github.com/PingCAP-QE/ee-apps/dl/gen/ks3"
)

// BuildDownloadObjectPayload builds the payload for the ks3 download-object
// endpoint from CLI flags.
func BuildDownloadObjectPayload(ks3DownloadObjectBucket string, ks3DownloadObjectKey string) (*ks3.DownloadObjectPayload, error) {
	var bucket string
	{
		bucket = ks3DownloadObjectBucket
	}
	var key string
	{
		key = ks3DownloadObjectKey
	}
	v := &ks3.DownloadObjectPayload{}
	v.Bucket = bucket
	v.Key = key

	return v, nil
}
