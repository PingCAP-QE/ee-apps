-- Manual setup for the TCMS-owned cost allocation database.
--
-- This script is intentionally kept under docs/ because it drops and recreates
-- tcms_cost.resource_allocation.
--
-- IMPORTANT: do not run this file as-is. Replace '<tcms_user>' and '<password>'
-- first; otherwise TiDB/MySQL will create a literal placeholder user.
-- Replace '<cost_insight_user>' too if you need to uncomment the optional
-- Cost Insight read grant.

CREATE DATABASE IF NOT EXISTS tcms_cost
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

DROP TABLE IF EXISTS tcms_cost.resource_allocation;

-- vendor_tags_json stores only tags with real values. Omit wildcard keys instead
-- of writing JSON null/empty values, for example use {"shared_pool":"pool-a"}
-- for pool-level records instead of {"cluster":null,"shared_pool":"pool-a"}.
CREATE TABLE tcms_cost.resource_allocation (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NULL,
  vendor_tags_json JSON NOT NULL,
  icost_owner_email VARCHAR(255) NULL,
  icost_service VARCHAR(255) NULL,
  icost_project VARCHAR(255) NULL,
  icost_service_exec_id VARCHAR(255) NULL,
  valid_from DATE NULL,
  valid_to DATE NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_resource_allocation_lookup (
    vendor,
    account_id,
    valid_from,
    valid_to
  ),
  KEY idx_resource_allocation_owner (icost_owner_email),
  KEY idx_resource_allocation_project (icost_project)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- Create a TCMS application SQL user when running as a TiDB admin/root user.
-- TCMS owns this database and can manage all objects in it.
CREATE USER IF NOT EXISTS '<tcms_user>'@'%' IDENTIFIED BY '<password>';

GRANT ALL PRIVILEGES
  ON tcms_cost.*
  TO '<tcms_user>'@'%';

-- Cost Insight uses its existing SQL user. If that user cannot read this table,
-- grant read access explicitly:
-- GRANT SELECT ON tcms_cost.resource_allocation TO '<cost_insight_user>'@'%';

-- Optional verification:
-- SHOW GRANTS FOR '<tcms_user>'@'%';
-- SHOW GRANTS FOR '<cost_insight_user>'@'%';
