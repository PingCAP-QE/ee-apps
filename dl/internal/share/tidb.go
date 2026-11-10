package share

import (
	"context"
	"database/sql"
	_ "embed"
	"errors"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"
)

//go:embed schema.sql
var Schema string

// TiDB stores delivery grants in the shared TiDB cluster using the MySQL
// protocol. All quota-changing transactions explicitly use TiDB pessimistic
// mode so multiple dl replicas cannot authorize the same final session.
type TiDB struct{ DB *sql.DB }

func Open(ctx context.Context, dsn string) (*TiDB, error) {
	cfg, err := mysql.ParseDSN(dsn)
	if err != nil || cfg.DBName == "" {
		return nil, errors.New("invalid delivery database configuration")
	}
	cfg.ParseTime = true
	cfg.Loc = time.UTC
	cfg.MultiStatements = false
	cfg.InterpolateParams = false
	connector, err := mysql.NewConnector(cfg)
	if err != nil {
		return nil, errors.New("invalid delivery database configuration")
	}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(16)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(30 * time.Minute)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, errors.New("delivery database unavailable")
	}
	return &TiDB{DB: db}, nil
}

// Migrate applies idempotent TiDB DDL statements one at a time. TiDB DDL is
// not transactional, so runtime identities must not call this method and a
// deployment must complete migration before starting either server mode.
func (s *TiDB) Migrate(ctx context.Context) error {
	for _, statement := range strings.Split(Schema, ";\n") {
		statement = strings.TrimSpace(statement)
		if statement == "" {
			continue
		}
		if _, err := s.DB.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	return nil
}

func (s *TiDB) IssuerEnabled(ctx context.Context, id string) (bool, error) {
	var enabled bool
	err := s.DB.QueryRowContext(ctx, `SELECT enabled FROM download_issuers WHERE id=?`, id).Scan(&enabled)
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

type pessimisticTx struct {
	ctx  context.Context
	conn *sql.Conn
	done bool
}

func (s *TiDB) beginPessimistic(ctx context.Context) (*pessimisticTx, error) {
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return nil, err
	}
	if _, err := conn.ExecContext(ctx, `BEGIN PESSIMISTIC`); err != nil {
		conn.Close()
		return nil, err
	}
	return &pessimisticTx{ctx: ctx, conn: conn}, nil
}

func (tx *pessimisticTx) Exec(query string, args ...any) (sql.Result, error) {
	return tx.conn.ExecContext(tx.ctx, query, args...)
}

func (tx *pessimisticTx) QueryRow(query string, args ...any) *sql.Row {
	return tx.conn.QueryRowContext(tx.ctx, query, args...)
}

func (tx *pessimisticTx) Commit() error {
	if tx.done {
		return nil
	}
	tx.done = true
	_, err := tx.conn.ExecContext(tx.ctx, `COMMIT`)
	closeErr := tx.conn.Close()
	if err != nil {
		return err
	}
	return closeErr
}

func (tx *pessimisticTx) Rollback() {
	if tx.done {
		return
	}
	tx.done = true
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, _ = tx.conn.ExecContext(ctx, `ROLLBACK`)
	_ = tx.conn.Close()
}

func (s *TiDB) Create(ctx context.Context, g *Grant, ttl int64) error {
	tx, err := s.beginPessimistic(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	// Use the TiDB clock for all replicas and persist the exact values returned
	// to the caller. Registry resolution happens before this transaction.
	if err := tx.QueryRow(`SELECT CURRENT_TIMESTAMP(6)`).Scan(&g.CreatedAt); err != nil {
		return err
	}
	g.ExpiresAt = g.CreatedAt.Add(time.Duration(ttl) * time.Second)
	res, err := tx.Exec(`INSERT INTO download_shares
	 (id,token_hash,repository,source_tag,manifest_digest,blob_digest,blob_size,media_type,filename,customer_id,purpose,issued_by,created_at,expires_at,max_sessions)
	 SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
	 WHERE EXISTS (SELECT 1 FROM download_issuers WHERE id=? AND enabled=TRUE)`,
		g.ID, g.TokenHash, g.Repository, g.SourceTag, g.ManifestDigest, g.BlobDigest, g.BlobSize, g.MediaType, g.Filename, g.CustomerID, g.Purpose, g.IssuedBy, g.CreatedAt, g.ExpiresAt, g.MaxSessions, g.IssuedBy)
	if err != nil {
		return err
	}
	rows, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if rows != 1 {
		return ErrNotFound
	}
	if _, err = tx.Exec(`INSERT INTO download_events(share_id,phase,outcome) VALUES(?,'issue','created')`, g.ID); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *TiDB) Get(ctx context.Context, id, issuer string) (*Grant, error) {
	return scanGrant(s.DB.QueryRowContext(ctx, `SELECT `+columns+` FROM download_shares WHERE id=? AND issued_by=?`, id, issuer))
}

func (s *TiDB) Revoke(ctx context.Context, id, issuer string) error {
	tx, err := s.beginPessimistic(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	res, err := tx.Exec(`UPDATE download_shares SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP(6)) WHERE id=? AND issued_by=?`, id, issuer)
	if err != nil {
		return err
	}
	rows, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if rows == 0 {
		var exists bool
		if err := tx.QueryRow(`SELECT EXISTS(SELECT 1 FROM download_shares WHERE id=? AND issued_by=?)`, id, issuer).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return ErrNotFound
		}
	}
	if _, err = tx.Exec(`INSERT INTO download_events(share_id,phase,outcome) VALUES(?,'revoke','revoked')`, id); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *TiDB) Authorize(ctx context.Context, hash []byte, reserve bool) (*Grant, error) {
	tx, err := s.beginPessimistic(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	phase := "head"
	if reserve {
		phase = "get"
	}
	// FOR UPDATE plus BEGIN PESSIMISTIC serializes concurrent GETs for a token
	// before checking and changing sessions_started.
	query := `SELECT ` + columns + ` FROM download_shares
	 WHERE token_hash=? AND revoked_at IS NULL AND expires_at>CURRENT_TIMESTAMP(6)
	 AND EXISTS (SELECT 1 FROM download_issuers WHERE id=issued_by AND enabled=TRUE)
	 AND (max_sessions IS NULL OR sessions_started<max_sessions)
	 FOR UPDATE`
	g, err := scanGrant(tx.QueryRow(query, hash))
	if errors.Is(err, ErrNotFound) {
		if _, err = tx.Exec(`INSERT INTO download_events(phase,outcome) VALUES(?,'denied')`, phase); err != nil {
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
	if reserve {
		if _, err = tx.Exec(`UPDATE download_shares SET sessions_started=sessions_started+1 WHERE id=?`, g.ID); err != nil {
			return nil, err
		}
		g.SessionsStarted++
	}
	if _, err = tx.Exec(`INSERT INTO download_events(share_id,phase,outcome) VALUES(?,?,'authorized')`, g.ID, phase); err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return g, nil
}

func (s *TiDB) Event(ctx context.Context, id, phase, outcome string, bytes int64) error {
	var shareID any
	if id != "" {
		shareID = id
	}
	_, err := s.DB.ExecContext(ctx, `INSERT INTO download_events(share_id,phase,outcome,bytes_transferred) VALUES(?,?,?,?)`, shareID, phase, outcome, bytes)
	return err
}
