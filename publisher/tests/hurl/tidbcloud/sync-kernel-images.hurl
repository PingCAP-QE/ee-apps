# POST /tidbcloud/sync-kernel-images
#
# Request to sync kernel images via ops platform kernel image build callback.
#
# Variables:
#   publisher_url  base URL of the publisher server (required)
#   stage          env stage: dev | prod (required)
#   image          source container image with tag, e.g.
#                  us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31
#
# Run:
#   hurl --test \
#     --variable publisher_url=http://localhost:8080 \
#     --variable stage=dev \
#     --variable image=us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.37 \
#     tests/hurl/tidbcloud/sync-kernel-images.hurl

POST {{publisher_url}}/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "stage": "{{stage}}",
  "images": ["{{image}}"]
}

HTTP 200
[Asserts]
jsonpath "$" isString
