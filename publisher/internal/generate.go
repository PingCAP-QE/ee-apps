//go:build !example
// +build !example

package internal

//go:generate go run -mod=mod goa.design/goa/v3/cmd/goa gen github.com/PingCAP-QE/ee-apps/publisher/internal/service/design -o ./service
