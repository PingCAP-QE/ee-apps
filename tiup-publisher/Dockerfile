FROM golang:1.23.1-alpine AS builder

COPY . /app
RUN --mount=type=cache,target=/go/pkg/mod cd /app && go build -o server ./cmd/server

# final image
# there are oras and tiup tools in the image.
FROM ghcr.io/pingcap-qe/cd/utils/release:v20240901-17-g6749b2e
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

COPY --from=builder --chown=root:root /app/server /app/server
ENTRYPOINT [ "/app/server" ]
