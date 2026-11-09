-- Apply once using a migration identity. Runtime identities do not need DDL.
CREATE TABLE IF NOT EXISTS download_issuers (
    id TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS download_shares (
    id UUID PRIMARY KEY,
    token_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    repository TEXT NOT NULL,
    source_tag TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    blob_digest TEXT NOT NULL,
    blob_size BIGINT NOT NULL CHECK (blob_size >= 0),
    media_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    issued_by TEXT NOT NULL REFERENCES download_issuers(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    max_sessions INTEGER CHECK (max_sessions IS NULL OR max_sessions > 0),
    sessions_started BIGINT NOT NULL DEFAULT 0 CHECK (sessions_started >= 0),
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS download_shares_expiry ON download_shares(expires_at);

-- Never include tokens, hashes, URLs, request headers or upstream errors here.
CREATE TABLE IF NOT EXISTS download_events (
    id BIGSERIAL PRIMARY KEY,
    share_id UUID,
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    bytes_transferred BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS download_events_created ON download_events(created_at);
