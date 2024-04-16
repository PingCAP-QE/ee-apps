# Copy 'website/' contents(not entire folder) into .(/webapp/) and build React 
FROM node:20.10.0 as webbuilder
WORKDIR /webapp
COPY website/ .
RUN --mount=type=cache,target=/webapp/node_modules npm install && yarn build

# Copy whole project's contents(not entire folder) into .(/goapp/) and build Golang
FROM golang:1.22.2 as serverbuilder
ENV GO111MODULE=on \
    CGO_ENABLED=0 \
    GOPROXY=https://goproxy.cn,direct
WORKDIR /goapp
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod go build -o ./bin/tibuild ./cmd/tibuild


FROM ghcr.io/pingcap-qe/bases/pingcap-base:v1.8.0
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

WORKDIR /app
COPY --from=webbuilder /webapp/build/ ./website/build/
COPY --from=serverbuilder /goapp/bin/tibuild ./bin/tibuild

CMD ["/app/bin/tibuild"]

