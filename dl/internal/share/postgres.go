package share

import (
	"context"
	"database/sql"
	_ "embed"
	"errors"
	"time"

	_ "github.com/lib/pq"
)

//go:embed schema.sql
var Schema string

type Postgres struct{ DB *sql.DB }

func Open(ctx context.Context, dsn string) (*Postgres, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, errors.New("invalid delivery database configuration")
	}
	db.SetMaxOpenConns(16)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(30 * time.Minute)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, errors.New("delivery database unavailable")
	}
	return &Postgres{DB: db}, nil
}

// Migrate is explicit and serializes concurrent migration jobs.
func (s *Postgres) Migrate(ctx context.Context) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(683150912)`); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, Schema); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Postgres) IssuerEnabled(ctx context.Context, id string) (bool, error) {
	var enabled bool
	err := s.DB.QueryRowContext(ctx, `SELECT enabled FROM download_issuers WHERE id=$1`, id).Scan(&enabled)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return enabled, err
}

const columns = `id, token_hash, repository, source_tag, manifest_digest, blob_digest, blob_size, media_type, filename, customer_id, purpose, issued_by, created_at, expires_at, revoked_at, max_sessions, sessions_started`

type scanner interface{ Scan(...any) error }

func scanGrant(row scanner) (*Grant, error) {
	var g Grant
	err := row.Scan(&g.ID, &g.TokenHash, &g.Repository, &g.SourceTag, &g.ManifestDigest, &g.BlobDigest, &g.BlobSize, &g.MediaType, &g.Filename, &g.CustomerID, &g.Purpose, &g.IssuedBy, &g.CreatedAt, &g.ExpiresAt, &g.RevokedAt, &g.MaxSessions, &g.SessionsStarted)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return &g, err
}

func (s *Postgres) Create(ctx context.Context, g *Grant, ttl int64) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	// Time comes from PostgreSQL, consistently across replicas. Resolve before
	// this transaction so registry latency never extends a grant's lifetime.
	err = tx.QueryRowContext(ctx, `INSERT INTO download_shares
	 (id,token_hash,repository,source_tag,manifest_digest,blob_digest,blob_size,media_type,filename,customer_id,purpose,issued_by,expires_at,max_sessions)
	 SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,now()+($13 * interval '1 second'),$14
	 WHERE EXISTS (SELECT 1 FROM download_issuers WHERE id=$12 AND enabled)
	 RETURNING created_at, expires_at`, g.ID, g.TokenHash, g.Repository, g.SourceTag, g.ManifestDigest, g.BlobDigest, g.BlobSize, g.MediaType, g.Filename, g.CustomerID, g.Purpose, g.IssuedBy, ttl, g.MaxSessions).Scan(&g.CreatedAt, &g.ExpiresAt)
	if errors.Is(err, sql.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO download_events(share_id,phase,outcome) VALUES($1,'issue','created')`, g.ID); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Postgres) Get(ctx context.Context, id, issuer string) (*Grant, error) {
	return scanGrant(s.DB.QueryRowContext(ctx, `SELECT `+columns+` FROM download_shares WHERE id=$1 AND issued_by=$2`, id, issuer))
}

func (s *Postgres) Revoke(ctx context.Context, id, issuer string) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	res, err := tx.ExecContext(ctx, `UPDATE download_shares SET revoked_at=COALESCE(revoked_at,now()) WHERE id=$1 AND issued_by=$2`, id, issuer)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return ErrNotFound
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO download_events(share_id,phase,outcome) VALUES($1,'revoke','revoked')`, id); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Postgres) Authorize(ctx context.Context, hash []byte, reserve bool) (*Grant, error) {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	where := `token_hash=$1 AND revoked_at IS NULL AND expires_at>clock_timestamp()
	 AND EXISTS (SELECT 1 FROM download_issuers WHERE id=issued_by AND enabled)
	 AND (max_sessions IS NULL OR sessions_started<max_sessions)`
	query := `SELECT ` + columns + ` FROM download_shares WHERE ` + where
	phase := "head"
	if reserve {
		phase = "get"
		query = `UPDATE download_shares SET sessions_started=sessions_started+1 WHERE ` + where + ` RETURNING ` + columns
	}
	g, err := scanGrant(tx.QueryRowContext(ctx, query, hash))
	if errors.Is(err, ErrNotFound) {
		if _, err = tx.ExecContext(ctx, `INSERT INTO download_events(phase,outcome) VALUES($1,'denied')`, phase); err != nil {
			return nil, err
		}
		if err = tx.Commit(); err != nil {
			return nil, err
		}
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO download_events(share_id,phase,outcome) VALUES($1,$2,'authorized')`, g.ID, phase); err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return g, nil
}

func (s *Postgres) Event(ctx context.Context, id, phase, outcome string, bytes int64) error {
	var shareID any
	if id != "" {
		shareID = id
	}
	_, err := s.DB.ExecContext(ctx, `INSERT INTO download_events(share_id,phase,outcome,bytes_transferred) VALUES($1,$2,$3,$4)`, shareID, phase, outcome, bytes)
	return err
}
