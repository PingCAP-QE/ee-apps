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
  "images": ["us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31"]
}

HTTP 400

# Missing images.
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
  "images": ["us.gcr.io/pingcap-public/tidbx/tikv:v8.5.4-nextgen.202510.31"]
}

HTTP 400

# image does not match the repo:tag pattern.
POST {{publisher_url}}/tidbcloud/sync-kernel-images
Content-Type: application/json
{
  "stage": "dev",
  "images": ["not an image"]
}

HTTP 400
