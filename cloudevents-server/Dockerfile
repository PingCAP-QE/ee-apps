FROM golang:1.23.4 AS builder
COPY . /app
RUN cd /app && go build -o server ./cmd/server

FROM debian:12
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

COPY --from=builder --chown=root:root /app/server /app/server
EXPOSE 8080
ENTRYPOINT [ "/app/server" ]
