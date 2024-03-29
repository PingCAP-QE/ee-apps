// Code generated by goa v3.14.1, DO NOT EDIT.
//
// oci HTTP client CLI support package
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package client

import (
	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
)

// BuildListFilesPayload builds the payload for the oci list-files endpoint
// from CLI flags.
func BuildListFilesPayload(ociListFilesRepository string, ociListFilesTag string) (*oci.ListFilesPayload, error) {
	var repository string
	{
		repository = ociListFilesRepository
	}
	var tag string
	{
		tag = ociListFilesTag
	}
	v := &oci.ListFilesPayload{}
	v.Repository = repository
	v.Tag = tag

	return v, nil
}

// BuildDownloadFilePayload builds the payload for the oci download-file
// endpoint from CLI flags.
func BuildDownloadFilePayload(ociDownloadFileRepository string, ociDownloadFileFile string, ociDownloadFileTag string) (*oci.DownloadFilePayload, error) {
	var repository string
	{
		repository = ociDownloadFileRepository
	}
	var file string
	{
		file = ociDownloadFileFile
	}
	var tag string
	{
		tag = ociDownloadFileTag
	}
	v := &oci.DownloadFilePayload{}
	v.Repository = repository
	v.File = file
	v.Tag = tag

	return v, nil
}
