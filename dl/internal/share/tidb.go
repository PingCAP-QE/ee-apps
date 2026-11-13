package share

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	dbent "github.com/PingCAP-QE/ee-apps/dl/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/dl/internal/database/ent/downloadissuer"
	"github.com/PingCAP-QE/ee-apps/dl/internal/database/ent/downloadshare"
	"github.com/PingCAP-QE/ee-apps/dl/internal/database/ent/migrate"
	"github.com/go-sql-driver/mysql"
)

// TiDB stores delivery grants in the shared TiDB cluster using the MySQL
// protocol. All quota-changing transactions explicitly use TiDB pessimistic
// mode so multiple dl replicas cannot authorize the same final session.
type TiDB struct {
	db     *sql.DB
	client *dbent.Client
}

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
	driver := entsql.OpenDB(dialect.MySQL, db)
	return &TiDB{db: db, client: dbent.NewClient(dbent.Driver(driver))}, nil
}

// Close releases the Ent client and its underlying connection pool.
func (s *TiDB) Close() error { return s.client.Close() }

// Migrate reconciles the TiDB schema from the Ent definitions. TiDB DDL is not
// transactional, so runtime identities must not call this method and a
// deployment must complete migration before starting either server mode.
func (s *TiDB) Migrate(ctx context.Context) error {
	return s.client.Schema.Create(ctx, migrate.WithDropColumn(false), migrate.WithDropIndex(false))
}

// Ready verifies that the migrated schema is queryable.
func (s *TiDB) Ready(ctx context.Context) error {
	_, err := s.client.DownloadIssuer.Query().Exist(ctx)
	return err
}

func (s *TiDB) IssuerEnabled(ctx context.Context, id string) (bool, error) {
	issuer, err := s.client.DownloadIssuer.Query().Where(downloadissuer.IDEQ(id)).Only(ctx)
	if dbent.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return issuer.Enabled, nil
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

func grantFromEnt(row *dbent.DownloadShare) *Grant {
	return &Grant{
		ID:        row.ID,
		TokenHash: row.TokenHash,
		Artifact: Artifact{
			Repository:     row.Repository,
			SourceTag:      row.SourceTag,
			ManifestDigest: row.ManifestDigest,
			BlobDigest:     row.BlobDigest,
			BlobSize:       row.BlobSize,
			MediaType:      row.MediaType,
			Filename:       row.Filename,
		},
		IssuedBy:        row.IssuedBy,
		CustomerID:      row.CustomerID,
		Purpose:         row.Purpose,
		CreatedAt:       row.CreatedAt,
		ExpiresAt:       row.ExpiresAt,
		RevokedAt:       row.RevokedAt,
		MaxSessions:     row.MaxSessions,
		SessionsStarted: int(row.SessionsStarted),
	}
}

type pessimisticTx struct {
	ctx  context.Context
	conn *sql.Conn
	done bool
}

func (s *TiDB) beginPessimistic(ctx context.Context) (*pessimisticTx, error) {
	conn, err := s.db.Conn(ctx)
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
	var now time.Time
	if err := s.db.QueryRowContext(ctx, `SELECT CURRENT_TIMESTAMP(6)`).Scan(&now); err != nil {
		return err
	}
	g.CreatedAt = now
	g.ExpiresAt = now.Add(time.Duration(ttl) * time.Second)
	tx, err := s.client.Tx(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	enabled, err := tx.DownloadIssuer.Query().
		Where(downloadissuer.IDEQ(g.IssuedBy), downloadissuer.EnabledEQ(true)).
		Exist(ctx)
	if err != nil {
		return err
	}
	if !enabled {
		return ErrNotFound
	}
	_, err = tx.DownloadShare.Create().
		SetID(g.ID).
		SetTokenHash(g.TokenHash).
		SetRepository(g.Repository).
		SetSourceTag(g.SourceTag).
		SetManifestDigest(g.ManifestDigest).
		SetBlobDigest(g.BlobDigest).
		SetBlobSize(g.BlobSize).
		SetMediaType(g.MediaType).
		SetFilename(g.Filename).
		SetCustomerID(g.CustomerID).
		SetPurpose(g.Purpose).
		SetIssuedBy(g.IssuedBy).
		SetCreatedAt(g.CreatedAt).
		SetExpiresAt(g.ExpiresAt).
		SetNillableMaxSessions(g.MaxSessions).
		Save(ctx)
	if err != nil {
		return err
	}
	if _, err = tx.DownloadEvent.Create().SetShareID(g.ID).SetPhase("issue").SetOutcome("created").Save(ctx); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *TiDB) Get(ctx context.Context, id, issuer string) (*Grant, error) {
	row, err := s.client.DownloadShare.Query().
		Where(downloadshare.IDEQ(id), downloadshare.IssuedByEQ(issuer)).
		Only(ctx)
	if dbent.IsNotFound(err) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return grantFromEnt(row), nil
}

func (s *TiDB) Revoke(ctx context.Context, id, issuer string) error {
	tx, err := s.client.Tx(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	row, err := tx.DownloadShare.Query().
		Where(downloadshare.IDEQ(id), downloadshare.IssuedByEQ(issuer)).
		Only(ctx)
	if dbent.IsNotFound(err) {
		return ErrNotFound
	}
	if err != nil {
		return err
	}
	if row.RevokedAt == nil {
		if _, err = row.Update().SetRevokedAt(time.Now().UTC()).Save(ctx); err != nil {
			return err
		}
	}
	if _, err = tx.DownloadEvent.Create().SetShareID(id).SetPhase("revoke").SetOutcome("revoked").Save(ctx); err != nil {
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
	builder := s.client.DownloadEvent.Create().
		SetPhase(phase).
		SetOutcome(outcome).
		SetBytesTransferred(bytes)
	if id != "" {
		builder.SetShareID(id)
	}
	_, err := builder.Save(ctx)
	return err
}
