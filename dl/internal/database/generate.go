package database

// Ent v0.14.6 is the repository-standard version. Its loader is not compatible
// with Go 1.27 export data yet, so code generation uses the module's Go version.
//go:generate env GOTOOLCHAIN=go1.25.8 go run -mod=mod entgo.io/ent/cmd/ent generate --target ./ent ./schema
