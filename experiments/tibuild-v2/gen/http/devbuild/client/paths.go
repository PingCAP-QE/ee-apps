// Code generated by goa v3.20.0, DO NOT EDIT.
//
// HTTP request path constructors for the devbuild service.
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/design

package client

import (
	"fmt"
)

// ListDevbuildPath returns the URL path to the devbuild service list HTTP endpoint.
func ListDevbuildPath() string {
	return "/api/devbuilds"
}

// CreateDevbuildPath returns the URL path to the devbuild service create HTTP endpoint.
func CreateDevbuildPath() string {
	return "/api/devbuilds"
}

// GetDevbuildPath returns the URL path to the devbuild service get HTTP endpoint.
func GetDevbuildPath(id int) string {
	return fmt.Sprintf("/api/devbuilds/%v", id)
}

// UpdateDevbuildPath returns the URL path to the devbuild service update HTTP endpoint.
func UpdateDevbuildPath(id int) string {
	return fmt.Sprintf("/api/devbuilds/%v", id)
}

// RerunDevbuildPath returns the URL path to the devbuild service rerun HTTP endpoint.
func RerunDevbuildPath(id int) string {
	return fmt.Sprintf("/api/devbuilds/%v/rerun", id)
}
