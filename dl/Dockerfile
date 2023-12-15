FROM golang:1.21.5 AS builder

COPY . /app
RUN --mount=type=cache,target=/go/pkg/mod cd /app && go build -o server ./cmd/server

FROM ghcr.io/pingcap-qe/bases/pingcap-base:v1.8.0
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

COPY --from=builder --chown=root:root /app/server /app/server
EXPOSE 8080
ENTRYPOINT [ "/app/server" ]