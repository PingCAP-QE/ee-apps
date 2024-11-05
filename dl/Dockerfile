FROM golang:1.23.2 AS builder

COPY . /app
RUN --mount=type=cache,target=/go/pkg/mod cd /app && go build -o server ./cmd/server

FROM ghcr.io/pingcap-qe/bases/pingcap-base:v1.9.2
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

COPY --from=builder --chown=root:root /app/server /app/server
EXPOSE 80
ENTRYPOINT [ "/app/server", "--domain=0.0.0.0:80" ]
