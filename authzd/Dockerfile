FROM golang:1.27.4-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/authzd ./cmd/authzd

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/authzd /authzd
USER 65532:65532
EXPOSE 9002 9003
ENTRYPOINT ["/authzd"]
