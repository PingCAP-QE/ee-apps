package internal

//go:generate go run -mod=mod entgo.io/ent/cmd/ent generate --target ./database/ent ./database/schema
//go:generate go run -mod=mod goa.design/goa/v3/cmd/goa gen github.com/PingCAP-QE/ee-apps/tibuild/internal/service/design -o ./service
