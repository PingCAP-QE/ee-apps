-- GKE PersistentVolume names (pvc-<uuid>) match GCP Billing Export's
-- resource.name. This retains only the attribution link across sync jobs.
CREATE TABLE IF NOT EXISTS cost_kubernetes_pvc_pod_mapping (
  id BIGINT NOT NULL AUTO_INCREMENT,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  persistent_volume_name VARCHAR(255) NOT NULL,
  pod_uid VARCHAR(255) NOT NULL,
  author VARCHAR(255) NULL,
  org VARCHAR(255) NULL,
  repo VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_kubernetes_pvc_pod_mapping (
    vendor,
    account_id,
    persistent_volume_name,
    pod_uid
  ),
  KEY idx_cost_kubernetes_pvc_pod_resource (
    vendor,
    account_id,
    persistent_volume_name
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
