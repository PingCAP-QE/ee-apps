# Hurl tests for the publisher server

Organized HTTP tests written with [Hurl](https://hurl.dev) (needs `hurl >= 8`).

## Layout

```
tests/hurl/tidbcloud/
  sync-kernel-images.hurl        # happy path for POST /tidbcloud/sync-kernel-images
  sync-kernel-images-errors.hurl # payload validation failure cases
```

## Run

The success case requires `publisher_url`, `stage`, `image` variables. Error
cases only need `publisher_url`.

```bash
# Against a local dev server
hurl --test \
  --variable publisher_url=http://localhost:8080 \
  --variable stage=dev \
  --variable image=us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31 \
  tests/hurl/tidbcloud/sync-kernel-images.hurl

# Run the whole tidbcloud suite
hurl --test --variable publisher_url=http://localhost:8080 \
  --variable stage=dev \
  --variable image=us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31 \
  tests/hurl/tidbcloud/*.hurl
```

`stage` accepts `dev` or `prod` per the design enum. Change `image` to the
kernel image you want to sync; the source image OCI labels
(`org.opencontainers.image.source/revision/ref.name`) are read by the server
to build the ops callback payload.
