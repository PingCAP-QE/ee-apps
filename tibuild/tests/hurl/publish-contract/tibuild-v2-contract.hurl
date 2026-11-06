# TiBuild-v2 publish-domain contract
#
# Same assertions as legacy-contract.hurl, addressed at the paths tibuild-v2
# serves. Everything is mounted under /api/v2, and the TiUP routes live in the
# publish service, so they are /api/v2/publish/tiup/*.
#
# Run against a local or port-forwarded tibuild-v2:
#   hurl --test --variable base_url=http://localhost:8080 tests/hurl/publish-contract/tibuild-v2-contract.hurl

# --- /tiup -------------------------------------------------------------------

# delivery-by-rules requires artifact_url.
POST {{base_url}}/api/v2/publish/tiup/delivery-by-rules
Content-Type: application/json
{}
HTTP 400
[Asserts]
jsonpath "$.name" == "missing_field"
jsonpath "$.fault" == false
jsonpath "$.message" contains "artifact_url"

# publish-request requires artifact_url and tiup_mirror.
POST {{base_url}}/api/v2/publish/tiup/publish-request
Content-Type: application/json
{}
HTTP 400
[Asserts]
jsonpath "$.name" == "missing_field"
jsonpath "$.message" contains "artifact_url"
jsonpath "$.message" contains "tiup_mirror"

# tiup_mirror is an enum of staging|prod.
POST {{base_url}}/api/v2/publish/tiup/publish-request
Content-Type: application/json
{
  "artifact_url": "oci.com/repo:tag",
  "tiup_mirror": "bad"
}
HTTP 400
[Asserts]
jsonpath "$.name" == "invalid_enum_value"
jsonpath "$.message" contains "staging"

# publish-request-single requires the from/publish pair.
POST {{base_url}}/api/v2/publish/tiup/publish-request-single
Content-Type: application/json
{}
HTTP 400
[Asserts]
jsonpath "$.name" == "missing_field"
jsonpath "$.message" contains "from"
jsonpath "$.message" contains "publish"

# Unknown request id is a fault (500), NOT 404. The path is not UUID-validated.
GET {{base_url}}/api/v2/publish/tiup/publish-request/00000000-0000-4000-8000-000000000000
HTTP 500
[Asserts]
jsonpath "$.name" == "fault"
jsonpath "$.fault" == true
jsonpath "$.message" contains "request ID not found"

GET {{base_url}}/api/v2/publish/tiup/publish-request/not-a-uuid
HTTP 500
[Asserts]
jsonpath "$.name" == "fault"
jsonpath "$.message" contains "request ID not found"

# --- /tidbcloud --------------------------------------------------------------

# cloudconfig requires stage and image.
POST {{base_url}}/api/v2/tidbcloud/devops/cloudconfig/versions/component
Content-Type: application/json
{}
HTTP 400
[Asserts]
jsonpath "$.name" == "missing_field"
jsonpath "$.message" contains "stage"
jsonpath "$.message" contains "image"

# tcms requires image.
POST {{base_url}}/api/v2/tidbcloud/tidbx-component-image-builds
Content-Type: application/json
{}
HTTP 400
[Asserts]
jsonpath "$.name" == "missing_field"
jsonpath "$.message" contains "image"

# commit_sha must be exactly 40 characters when github info is supplied.
POST {{base_url}}/api/v2/tidbcloud/tidbx-component-image-builds
Content-Type: application/json
{
  "image": "a/b:c",
  "github": {
    "full_repo": "a/b",
    "commit_sha": "short"
  }
}
HTTP 400
[Asserts]
jsonpath "$.name" == "invalid_length"
jsonpath "$.message" contains "commit_sha"

# sync-kernel-images requires stage and images; stage is dev|prod.
POST {{base_url}}/api/v2/tidbcloud/sync-kernel-images
Content-Type: application/json
{}
HTTP 400
[Asserts]
jsonpath "$.name" == "missing_field"
jsonpath "$.message" contains "stage"
jsonpath "$.message" contains "images"

POST {{base_url}}/api/v2/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "stage": "staging",
  "images": ["us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31"]
}
HTTP 400
[Asserts]
jsonpath "$.name" == "invalid_enum_value"
jsonpath "$.message" contains "dev"

POST {{base_url}}/api/v2/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "stage": "dev",
  "images": ["not an image"]
}
HTTP 400
[Asserts]
jsonpath "$.name" == "invalid_pattern"
jsonpath "$.message" contains "images"
