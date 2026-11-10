package share

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/PingCAP-QE/ee-apps/dl/internal/database/ent/downloadevent"
	"github.com/go-sql-driver/mysql"
	"github.com/google/uuid"
)

func testTiDB(t *testing.T) (*TiDB, string) {
	t.Helper()
	dsn := os.Getenv("DL_TEST_TIDB_DSN")
	if dsn == "" {
		t.Skip("set DL_TEST_TIDB_DSN to a disposable TiDB instance")
	}
	cfg, err := mysql.ParseDSN(dsn)
	if err != nil {
		t.Fatal(err)
	}
	database := "dl_test_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	adminCfg := cfg.Clone()
	adminCfg.DBName = ""
	connector, err := mysql.NewConnector(adminCfg)
	if err != nil {
		t.Fatal(err)
	}
	admin := sql.OpenDB(connector)
	ctx := context.Background()
	if _, err = admin.ExecContext(ctx, `CREATE DATABASE `+database); err != nil {
		admin.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := admin.ExecContext(ctx, `DROP DATABASE `+database); err != nil {
			t.Error(err)
		}
		admin.Close()
	})
	testCfg := cfg.Clone()
	testCfg.DBName = database
	isolatedDSN := testCfg.FormatDSN()
	s, err := Open(ctx, isolatedDSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	if err = s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err = s.client.DownloadIssuer.CreateBulk(
		s.client.DownloadIssuer.Create().SetID("issuer").SetEnabled(true),
		s.client.DownloadIssuer.Create().SetID("other").SetEnabled(true),
	).Save(ctx); err != nil {
		t.Fatal(err)
	}
	return s, isolatedDSN
}

func testGrant(t *testing.T, s *TiDB, max *int) (*Grant, []byte) {
	t.Helper()
	token := uuid.NewString() + uuid.NewString()
	h := sha256.Sum256([]byte(token))
	g := &Grant{ID: uuid.NewString(), TokenHash: h[:], IssuedBy: "issuer", Purpose: "customer-delivery", MaxSessions: max, Artifact: Artifact{Repository: "registry.example.com/releases/tidb", SourceTag: "v8.1.0", ManifestDigest: "sha256:" + hex.EncodeToString(h[:]), BlobDigest: "sha256:" + hex.EncodeToString(h[:]), BlobSize: 10, Filename: "tidb.tar.gz", MediaType: "application/octet-stream"}}
	if err := s.Create(context.Background(), g, 86400); err != nil {
		t.Fatal(err)
	}
	return g, h[:]
}

func TestTiDBQuotaAtomicAcrossReplicas(t *testing.T) {
	s, dsn := testTiDB(t)
	replica, err := Open(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer replica.Close()
	max := 1
	g, hash := testGrant(t, s, &max)
	for range 3 {
		if _, err := replica.Authorize(context.Background(), hash, false); err != nil {
			t.Fatal(err)
		}
	}
	var allowed atomic.Int64
	var wg sync.WaitGroup
	for i := range 32 {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			store := s
			if i%2 == 0 {
				store = replica
			}
			_, err := store.Authorize(context.Background(), hash, true)
			if err == nil {
				allowed.Add(1)
			} else if !errors.Is(err, ErrNotFound) {
				t.Errorf("authorize: %v", err)
			}
		}(i)
	}
	wg.Wait()
	if allowed.Load() != 1 {
		t.Fatalf("quota overrun: %d", allowed.Load())
	}
	if _, err := s.Authorize(context.Background(), hash, false); !errors.Is(err, ErrNotFound) {
		t.Fatal("HEAD bypassed exhausted quota")
	}
	stored, err := replica.Get(context.Background(), g.ID, "issuer")
	if err != nil || stored.SessionsStarted != 1 {
		t.Fatal("incorrect durable quota")
	}
	var count int
	count, err = s.client.DownloadEvent.Query().Where(
		downloadevent.ShareIDEQ(g.ID),
		downloadevent.PhaseEQ("get"),
		downloadevent.OutcomeEQ("authorized"),
	).Count(context.Background())
	if err != nil || count != 1 {
		t.Fatalf("authorization not audited atomically: count=%d err=%v", count, err)
	}
}

func TestTiDBRevocationExpiryAndIssuerDisable(t *testing.T) {
	for _, state := range []string{"revoked", "expired", "issuer-disabled"} {
		t.Run(state, func(t *testing.T) {
			s, dsn := testTiDB(t)
			g, hash := testGrant(t, s, nil)
			replica, err := Open(context.Background(), dsn)
			if err != nil {
				t.Fatal(err)
			}
			defer replica.Close()
			if _, err = replica.Authorize(context.Background(), hash, false); err != nil {
				t.Fatal(err)
			}
			switch state {
			case "revoked":
				err = s.Revoke(context.Background(), g.ID, "issuer")
			case "expired":
				_, err = s.db.Exec(`UPDATE download_shares SET created_at=DATE_SUB(CURRENT_TIMESTAMP(6), INTERVAL 2 SECOND), expires_at=DATE_SUB(CURRENT_TIMESTAMP(6), INTERVAL 1 SECOND) WHERE id=?`, g.ID)
			case "issuer-disabled":
				_, err = s.client.DownloadIssuer.UpdateOneID("issuer").SetEnabled(false).Save(context.Background())
			}
			if err != nil {
				t.Fatal(err)
			}
			for _, reserve := range []bool{false, true} {
				if _, err := replica.Authorize(context.Background(), hash, reserve); !errors.Is(err, ErrNotFound) {
					t.Fatalf("%s accepted: %v", state, err)
				}
			}
		})
	}
}

func TestTiDBDurabilityOwnershipAndMigration(t *testing.T) {
	s, dsn := testTiDB(t)
	g, hash := testGrant(t, s, nil)
	if _, err := s.Get(context.Background(), g.ID, "other"); !errors.Is(err, ErrNotFound) {
		t.Fatal("cross-issuer read")
	}
	if err := s.Revoke(context.Background(), g.ID, "other"); !errors.Is(err, ErrNotFound) {
		t.Fatal("cross-issuer revocation")
	}
	if err := s.Migrate(context.Background()); err != nil {
		t.Fatal("migration is not idempotent", err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	restarted, err := Open(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	if _, err := restarted.Authorize(context.Background(), hash, true); err != nil {
		t.Fatal("grant lost on restart", err)
	}
	if _, err := restarted.client.DownloadIssuer.UpdateOneID("issuer").SetEnabled(false).Save(context.Background()); err != nil {
		t.Fatal(err)
	}
	g.ID = uuid.NewString()
	g.TokenHash = sha256Bytes("different")
	if err := restarted.Create(context.Background(), g, 86400); !errors.Is(err, ErrNotFound) {
		t.Fatal("disabled issuer created grant", err)
	}
}

func sha256Bytes(s string) []byte {
	h := sha256.Sum256([]byte(s))
	return h[:]
}
