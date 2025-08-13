FROM golang:1.25.0-alpine AS builder

COPY . /ws
WORKDIR /ws
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -o bin/publisher ./cmd/publisher && \
    go build -o bin/publisher-cli ./cmd/publisher-cli && \
    go build -o bin/worker ./cmd/worker

# final image
# there tiup tools in the image, we need to call it in worker.
FROM ghcr.io/pingcap-qe/cd/utils/release:v20240901-17-g6749b2e
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

COPY --from=builder --chown=root:root /ws/bin /app
ENTRYPOINT ["/app/worker"]
