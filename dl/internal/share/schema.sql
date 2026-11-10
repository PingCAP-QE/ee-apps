-- Apply once using a migration identity. Runtime identities do not need DDL.
-- TiDB schema. Apply through `dl --server-mode=delivery-migrate` before
-- starting public or management replicas.
CREATE TABLE IF NOT EXISTS download_issuers (
    id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS download_shares (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    token_hash BINARY(32) NOT NULL UNIQUE,
    repository VARCHAR(512) NOT NULL,
    source_tag VARCHAR(128) NOT NULL,
    manifest_digest VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    blob_digest VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    blob_size BIGINT NOT NULL CHECK (blob_size >= 0),
    media_type VARCHAR(255) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    customer_id VARCHAR(255) NOT NULL,
    purpose VARCHAR(64) NOT NULL,
    issued_by VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    max_sessions INT NULL CHECK (max_sessions IS NULL OR max_sessions > 0),
    sessions_started BIGINT NOT NULL DEFAULT 0 CHECK (sessions_started >= 0),
    CHECK (expires_at > created_at),
    INDEX download_shares_expiry (expires_at),
    INDEX download_shares_issuer (issued_by)
);

-- Never include tokens, hashes, URLs, request headers or upstream errors here.
CREATE TABLE IF NOT EXISTS download_events (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    share_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    phase VARCHAR(32) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    bytes_transferred BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX download_events_created (created_at),
    INDEX download_events_share (share_id)
);
