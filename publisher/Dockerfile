FROM golang:1.23.4-alpine AS builder

COPY . /app
RUN --mount=type=cache,target=/go/pkg/mod cd /app && \
    go build -o bin/publisher ./cmd/publisher && \
    go build -o bin/publisher-cli ./cmd/publisher-cli && \
    go build -o bin/worker ./cmd/worker

# final image
# there are oras and tiup tools in the image.
FROM ghcr.io/pingcap-qe/cd/utils/release:v20240901-17-g6749b2e
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

COPY --from=builder --chown=root:root /app/bin /app
ENTRYPOINT ["/app/worker"]
