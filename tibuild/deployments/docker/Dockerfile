# -- Tips: When 'FROM', many are allowed, and '--from=number' or '--from=name' is widely used to aggregate sources
# -- Tips: When 'WORKDIR', it is widely used in dockerfile to switch directories. And if the directory not exist, will also create it
# -- Tips: When 'COPY', only the files inside the source folder are copied(not entire folder),
# And if you want to copy the entire folder, then keep the dest folder name is same as the source, make it look like a complete copy.

# Copy 'website/' contents(not entire folder) into .(/webapp/) and build React 
FROM node:14.15 as webbuilder
WORKDIR /webapp
COPY website/ .
RUN yarn build

# Copy whole project's contents(not entire folder) into .(/goapp/) and build Golang
FROM golang as serverbuilder
ENV GO111MODULE=on \
    CGO_ENABLED=0 \
    GOPROXY=https://goproxy.cn,direct
WORKDIR /goapp
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go test ./... && go build -o ./bin/tibuild ./cmd/tibuild


FROM hub.pingcap.net/bases/pingcap-base:v1

WORKDIR /app
COPY --from=webbuilder /webapp/build/ ./website/build/
COPY --from=serverbuilder /goapp/bin/tibuild ./bin/tibuild

CMD ["/app/bin/tibuild"]

