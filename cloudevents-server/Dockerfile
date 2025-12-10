FROM golang:1.25.5 AS builder
COPY . /app
RUN cd /app && go build -o server ./cmd/server

FROM debian:13
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder --chown=root:root /app/server /app/server
EXPOSE 8080
ENTRYPOINT [ "/app/server" ]
