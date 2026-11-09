package design

//go:generate sh -c "cd .. && go run goa.design/goa/v3/cmd/goa gen github.com/PingCAP-QE/ee-apps/dl/design"
// Goa JSON output omits the newline required by this repository's hooks.
//go:generate sh -c "echo >> ../gen/http/openapi.json && echo >> ../gen/http/openapi3.json"
