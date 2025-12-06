FROM golang:1.25.5-alpine AS builder

COPY . /ws
WORKDIR /ws
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -o bin/worker ./cmd/worker

# final image
# there tiup tools in the image, we need to call it in worker.
FROM ghcr.io/pingcap-qe/cd/utils/release:v2025.10.26-7-geb77a69
LABEL org.opencontainers.image.title="PingCAP Publisher Worker"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"
LABEL org.opencontainers.image.url="https://github.com/PingCAP-QE/ee-apps/tree/main/publisher"

COPY --from=builder --chown=root:root /ws/bin/worker /app/worker
ENTRYPOINT ["/app/worker"]
