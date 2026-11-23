package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"time"

	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	dbent "github.com/PingCAP-QE/ee-apps/delivery-approval/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/database/ent/migrate"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/database/ent/request"
	"github.com/go-sql-driver/mysql"
	"github.com/google/uuid"
)

// TiDB stores approval requests in the shared TiDB cluster using the MySQL
// protocol. All state-changing queries are conditional updates; there is no
// read-modify-write on the approval status.
type TiDB struct {
	db     *sql.DB
	client *dbent.Client
}

func Open(ctx context.Context, dsn string) (*TiDB, error) {
	cfg, err := mysql.ParseDSN(dsn)
	if err != nil || cfg.DBName == "" {
		return nil, errors.New("invalid approval database configuration")
	}
	cfg.ParseTime = true
	cfg.Loc = time.UTC
	cfg.MultiStatements = false
	cfg.InterpolateParams = false
	connector, err := mysql.NewConnector(cfg)
	if err != nil {
		return nil, errors.New("invalid approval database configuration")
	}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(16)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(30 * time.Minute)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, errors.New("approval database unavailable")
	}
	driver := entsql.OpenDB(dialect.MySQL, db)
	return &TiDB{db: db, client: dbent.NewClient(dbent.Driver(driver))}, nil
}

func (s *TiDB) Close() error { return s.client.Close() }

// Migrate reconciles the schema. TiDB DDL is not transactional, so only the
// one-shot migration command calls this; runtime startup never does.
func (s *TiDB) Migrate(ctx context.Context) error {
	return s.client.Schema.Create(ctx, migrate.WithDropColumn(false), migrate.WithDropIndex(false))
}

func (s *TiDB) Ready(ctx context.Context) error {
	_, err := s.client.Request.Query().Limit(1).Exist(ctx)
	return err
}

func (s *TiDB) CreateRequest(ctx context.Context, req *Request) error {
	if req.ID == "" {
		req.ID = uuid.NewString()
	}
	if req.CreatedAt.IsZero() {
		req.CreatedAt = time.Now().UTC()
	}
	if req.ExpiresAt.IsZero() {
		req.ExpiresAt = req.CreatedAt.Add(time.Duration(req.TTLSeconds) * time.Second)
	}
	if req.Status == "" {
		req.Status = StatusPending
	}

	tx, err := s.client.Tx(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if _, err = tx.Request.Create().
		SetID(req.ID).
		SetRequesterEmail(req.RequesterEmail).
		SetRequesterSub(req.RequesterSub).
		SetPurpose(req.Purpose).
		SetNote(req.Note).
		SetStatus(request.Status(req.Status)).
		SetTTLSeconds(req.TTLSeconds).
		SetCreatedAt(req.CreatedAt).
		SetExpiresAt(req.ExpiresAt).
		SetDeliveryStatus(request.DeliveryStatusPending).
		Save(ctx); err != nil {
		return err
	}

	req.Files = append([]File(nil), req.Files...)
	for i := range req.Files {
		f := &req.Files[i]
		if f.ID == "" {
			f.ID = uuid.NewString()
		}
		f.RequestID = req.ID
		if f.CreatedAt.IsZero() {
			f.CreatedAt = req.CreatedAt
		}
		dedupe := sha256.Sum256([]byte(f.Repository + "\x00" + f.Tag + "\x00" + f.Filename))
		if _, err = tx.RequestFile.Create().
			SetID(f.ID).
			SetRequestID(req.ID).
			SetRepository(f.Repository).
			SetTag(f.Tag).
			SetFilename(f.Filename).
			SetDedupeHash(dedupe[:]).
			SetCreatedAt(f.CreatedAt).
			Save(ctx); err != nil {
			return err
		}
	}

	if _, err = tx.RequestEvent.Create().
		SetRequestID(req.ID).
		SetAction("created").
		SetOutcome("ok").
		SetCreatedAt(req.CreatedAt).
		Save(ctx); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *TiDB) GetRequest(ctx context.Context, id string) (*Request, error) {
	row, err := s.client.Request.Query().
		Where(request.IDEQ(id)).
		WithFiles().
		Only(ctx)
	if dbent.IsNotFound(err) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return requestFromEnt(row), nil
}

func (s *TiDB) ListByRequester(ctx context.Context, email string, limit int) ([]*Request, error) {
	rows, err := s.client.Request.Query().
		Where(request.RequesterEmailEQ(email)).
		WithFiles().
		Order(dbent.Desc(request.FieldCreatedAt)).
		Limit(clampLimit(limit)).
		All(ctx)
	if err != nil {
		return nil, err
	}
	return requestsFromEnt(rows), nil
}

func (s *TiDB) ListPending(ctx context.Context, limit int) ([]*Request, error) {
	rows, err := s.client.Request.Query().
		Where(request.StatusEQ(request.StatusPending)).
		WithFiles().
		Order(dbent.Asc(request.FieldCreatedAt)).
		Limit(clampLimit(limit)).
		All(ctx)
	if err != nil {
		return nil, err
	}
	return requestsFromEnt(rows), nil
}

func (s *TiDB) Transition(ctx context.Context, id string, from, to Status, actor, note string, decidedAt time.Time) (bool, error) {
	if !from.Valid() || !to.Valid() {
		return false, errors.New("invalid approval transition")
	}
	if decidedAt.IsZero() {
		decidedAt = time.Now().UTC()
	}
	update := s.client.Request.Update().
		Where(
			request.IDEQ(id),
			request.StatusEQ(request.Status(from)),
		).
		SetStatus(request.Status(to))
	switch to {
	case StatusApproved, StatusRejected:
		update.SetDecidedAt(decidedAt).SetApproverEmail(actor).SetDecisionNote(note)
	case StatusRevoked, StatusExpired:
		update.SetDecidedAt(decidedAt)
	}
	n, err := update.Save(ctx)
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

func (s *TiDB) SetDelivery(ctx context.Context, id string, status DeliveryStatus, attempts int, errorClass string) error {
	_, err := s.client.Request.UpdateOneID(id).
		SetDeliveryStatus(request.DeliveryStatus(status)).
		SetDeliveryAttempts(attempts).
		SetLastErrorClass(errorClass).
		Save(ctx)
	return err
}

func (s *TiDB) AppendEvent(ctx context.Context, event *Event) error {
	if event.CreatedAt.IsZero() {
		event.CreatedAt = time.Now().UTC()
	}
	builder := s.client.RequestEvent.Create().
		SetRequestID(event.RequestID).
		SetAction(event.Action).
		SetOutcome(event.Outcome).
		SetCreatedAt(event.CreatedAt)
	if event.FileID != "" {
		builder.SetFileID(event.FileID)
	}
	if event.ActorEmail != "" {
		builder.SetActorEmail(event.ActorEmail)
	}
	if event.ErrorClass != "" {
		builder.SetErrorClass(event.ErrorClass)
	}
	_, err := builder.Save(ctx)
	return err
}

func (s *TiDB) SaveFileCiphertext(ctx context.Context, fileID string, ciphertext []byte) error {
	_, err := s.client.RequestFile.UpdateOneID(fileID).
		SetDownloadURLCiphertext(ciphertext).
		Save(ctx)
	return err
}

func (s *TiDB) ClearFileCiphertext(ctx context.Context, fileID string) error {
	_, err := s.client.RequestFile.UpdateOneID(fileID).
		ClearDownloadURLCiphertext().
		Save(ctx)
	return err
}

func requestFromEnt(row *dbent.Request) *Request {
	out := &Request{
		ID:               row.ID,
		RequesterEmail:   row.RequesterEmail,
		RequesterSub:     row.RequesterSub,
		Purpose:          row.Purpose,
		Note:             row.Note,
		Status:           Status(row.Status),
		TTLSeconds:       row.TTLSeconds,
		CreatedAt:        row.CreatedAt,
		ExpiresAt:        row.ExpiresAt,
		DecidedAt:        row.DecidedAt,
		ApproverEmail:    row.ApproverEmail,
		DecisionNote:     row.DecisionNote,
		DeliveryStatus:   DeliveryStatus(row.DeliveryStatus),
		DeliveryAttempts: row.DeliveryAttempts,
		LastErrorClass:   row.LastErrorClass,
	}
	for _, f := range row.Edges.Files {
		out.Files = append(out.Files, File{
			ID:                    f.ID,
			RequestID:             f.RequestID,
			Repository:            f.Repository,
			Tag:                   f.Tag,
			Filename:              f.Filename,
			ShareID:               f.ShareID,
			DownloadURLCiphertext: f.DownloadURLCiphertext,
			DeliveredAt:           f.DeliveredAt,
			CreatedAt:             f.CreatedAt,
		})
	}
	return out
}

func requestsFromEnt(rows []*dbent.Request) []*Request {
	out := make([]*Request, 0, len(rows))
	for _, row := range rows {
		out = append(out, requestFromEnt(row))
	}
	return out
}

func clampLimit(limit int) int {
	if limit < 1 {
		return 50
	}
	if limit > 200 {
		return 200
	}
	return limit
}
