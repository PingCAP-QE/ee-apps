FROM golang:1.21.1 AS builder

RUN go build -o server

FROM debian:12
COPY --from=builder --chown=root:root server /app/server
EXPOSE 8080
ENTRYPOINT [ "/app/server" ]
