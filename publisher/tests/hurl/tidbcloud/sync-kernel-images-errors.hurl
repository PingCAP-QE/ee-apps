# POST /tidbcloud/sync-kernel-images - validation error cases
#
# All requests below must be rejected with 400 by the server (Goa payload
# validation: stage enum + required fields, image pattern + required).

# Missing both required fields.
POST {{publisher_url}}/tidbcloud/sync-kernel-images
Content-Type: application/json
{}

HTTP 400

# Missing stage.
POST {{publisher_url}}/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "image": "us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31"
}

HTTP 400

# Missing image.
POST {{publisher_url}}/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "stage": "dev"
}

HTTP 400

# stage not in enum (dev, prod).
POST {{publisher_url}}/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "stage": "staging",
  "image": "us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31"
}

HTTP 400

# image does not match the repo:tag pattern.
POST {{publisher_url}}/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "stage": "dev",
  "image": "not an image"
}

HTTP 400
