package share

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"
)

func testPostgres(t *testing.T) (*Postgres, string) {
	t.Helper()
	dsn := os.Getenv("DL_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("set DL_TEST_POSTGRES_DSN to a disposable PostgreSQL database")
	}
	ctx := context.Background()
	admin, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	schema := "dl_test_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err = admin.DB.ExecContext(ctx, `CREATE SCHEMA `+schema); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, err := admin.DB.ExecContext(ctx, `DROP SCHEMA `+schema+` CASCADE`)
		if err != nil {
			t.Error(err)
		}
		admin.DB.Close()
	})
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	q := u.Query()
	q.Set("search_path", schema)
	u.RawQuery = q.Encode()
	isolated := u.String()
	s, err := Open(ctx, isolated)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.DB.Close() })
	if err = s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err = s.DB.ExecContext(ctx, `INSERT INTO download_issuers(id,enabled) VALUES('issuer',true),('other',true)`); err != nil {
		t.Fatal(err)
	}
	return s, isolated
}

func testGrant(t *testing.T, s *Postgres, max *int) (*Grant, []byte) {
	t.Helper()
	token := uuid.NewString() + uuid.NewString()
	h := sha256.Sum256([]byte(token))
	g := &Grant{ID: uuid.NewString(), TokenHash: h[:], IssuedBy: "issuer", Purpose: "customer-delivery", MaxSessions: max, Artifact: Artifact{Repository: "registry.example.com/releases/tidb", SourceTag: "v8.1.0", ManifestDigest: "sha256:" + hex.EncodeToString(h[:]), BlobDigest: "sha256:" + hex.EncodeToString(h[:]), BlobSize: 10, Filename: "tidb.tar.gz", MediaType: "application/octet-stream"}}
	if err := s.Create(context.Background(), g, 86400); err != nil {
		t.Fatal(err)
	}
	return g, h[:]
}

func TestPostgresQuotaAtomicAcrossReplicas(t *testing.T) {
	s, dsn := testPostgres(t)
	replica, err := Open(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer replica.DB.Close()
	max := 1
	g, hash := testGrant(t, s, &max)
	for i := 0; i < 3; i++ {
		if _, err := replica.Authorize(context.Background(), hash, false); err != nil {
			t.Fatal(err)
		}
	}
	var allowed atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
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
	if err := s.DB.QueryRow(`SELECT count(*) FROM download_events WHERE share_id=$1 AND phase='get' AND outcome='authorized'`, g.ID).Scan(&count); err != nil || count != 1 {
		t.Fatalf("authorization not audited atomically: count=%d err=%v", count, err)
	}
}

func TestPostgresRevocationExpiryAndIssuerDisable(t *testing.T) {
	for _, state := range []string{"revoked", "expired", "issuer-disabled"} {
		t.Run(state, func(t *testing.T) {
			s, dsn := testPostgres(t)
			g, hash := testGrant(t, s, nil)
			replica, err := Open(context.Background(), dsn)
			if err != nil {
				t.Fatal(err)
			}
			defer replica.DB.Close()
			if _, err = replica.Authorize(context.Background(), hash, false); err != nil {
				t.Fatal(err)
			}
			switch state {
			case "revoked":
				err = s.Revoke(context.Background(), g.ID, "issuer")
			case "expired":
				_, err = s.DB.Exec(`UPDATE download_shares SET created_at=now()-interval '2 seconds',expires_at=now()-interval '1 second' WHERE id=$1`, g.ID)
			case "issuer-disabled":
				_, err = s.DB.Exec(`UPDATE download_issuers SET enabled=false WHERE id='issuer'`)
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

func TestPostgresDurabilityOwnershipAndMigration(t *testing.T) {
	s, dsn := testPostgres(t)
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
	if err := s.DB.Close(); err != nil {
		t.Fatal(err)
	}
	restarted, err := Open(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.DB.Close()
	if _, err := restarted.Authorize(context.Background(), hash, true); err != nil {
		t.Fatal("grant lost on restart", err)
	}
	if _, err := restarted.DB.Exec(`UPDATE download_issuers SET enabled=false WHERE id='issuer'`); err != nil {
		t.Fatal(err)
	}
	g.ID = uuid.NewString()
	g.TokenHash = sha256Bytes("different")
	if err := restarted.Create(context.Background(), g, 86400); !errors.Is(err, ErrNotFound) {
		t.Fatal("disabled issuer created grant", err)
	}
}
func sha256Bytes(s string) []byte { h := sha256.Sum256([]byte(s)); return h[:] }
