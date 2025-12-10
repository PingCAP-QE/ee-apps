# Copy 'website/' contents(not entire folder) into .(/webapp/) and build React
FROM node:20.19.5 as webbuilder
WORKDIR /webapp
COPY website/ .
RUN --mount=type=cache,target=/webapp/node_modules npm install && yarn build

# Copy whole project's contents(not entire folder) into .(/goapp/) and build Golang
FROM golang:1.25.5 as serverbuilder
ENV CGO_ENABLED=0
WORKDIR /goapp
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod go build -o ./bin/tibuild ./cmd/tibuild

# final image.
FROM ghcr.io/pingcap-qe/bases/pingcap-base:v1.10.0
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

WORKDIR /app
COPY --from=webbuilder /webapp/build/ ./website/build/
COPY --from=serverbuilder /goapp/bin/tibuild ./bin/tibuild

CMD ["/app/bin/tibuild"]
