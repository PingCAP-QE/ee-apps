package openfga

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/relationships"
	fga "github.com/openfga/go-sdk"
	"github.com/openfga/go-sdk/client"
	"github.com/openfga/go-sdk/credentials"
)

// defaultTupleBatch is the OpenFGA write batch limit.
const defaultTupleBatch = 100

// TupleStore implements relationships.Store against one OpenFGA store. It pins the
// authorization model on every write so a tuple is never written by a job that
// read a different model.
type TupleStore struct {
	client  *client.OpenFgaClient
	storeID string
	modelID string
	batch   int
}

// NewTupleStore reuses the checker's configuration contract: a store ID and a
// pinned model ID are mandatory, and the API URL must be a plain HTTP(S)
// endpoint without embedded credentials.
func NewTupleStore(cfg Config) (*TupleStore, error) {
	if cfg.APIURL == "" || cfg.StoreID == "" || cfg.ModelID == "" {
		return nil, errors.New("OpenFGA API URL, store ID, and pinned model ID are required")
	}
	apiURL, err := url.Parse(cfg.APIURL)
	if err != nil || apiURL.Host == "" || (apiURL.Scheme != "http" && apiURL.Scheme != "https") || apiURL.User != nil || apiURL.RawQuery != "" || apiURL.Fragment != "" {
		return nil, errors.New("OpenFGA API URL must be an HTTP(S) endpoint without credentials or query parameters")
	}
	var creds *credentials.Credentials
	if cfg.APIToken != "" {
		creds, err = credentials.NewCredentials(credentials.Credentials{
			Method: credentials.CredentialsMethodApiToken,
			Config: &credentials.Config{ApiToken: cfg.APIToken},
		})
		if err != nil {
			return nil, fmt.Errorf("configure OpenFGA credentials: %w", err)
		}
	}
	fgaClient, err := client.NewSdkClient(&client.ClientConfiguration{
		ApiUrl: cfg.APIURL, StoreId: cfg.StoreID,
		Credentials: creds, RetryParams: &fga.RetryParams{MaxRetry: 2, MinWaitInMs: 50},
	})
	if err != nil {
		return nil, fmt.Errorf("create OpenFGA client: %w", err)
	}
	return &TupleStore{client: fgaClient, storeID: cfg.StoreID, modelID: cfg.ModelID, batch: defaultTupleBatch}, nil
}

// WithBatchSize overrides the write batch size. It exists for tests and for
// servers configured with a smaller limit.
func (s *TupleStore) WithBatchSize(size int) *TupleStore {
	if size > 0 {
		s.batch = size
	}
	return s
}

// Read returns every tuple of one object type, following pagination.
//
// The deployed OpenFGA rejects a type-only query (`object: "type:"`), so this
// reads without a filter and keeps the matching object type client-side. That is
// more traffic than a filtered read; it is bounded by the store's size, and the
// alternative (enumerating object ids first) cannot be derived from the scope.
func (s *TupleStore) Read(ctx context.Context, objectType string) ([]relationships.Tuple, error) {
	if s == nil || s.client == nil {
		return nil, errors.New("OpenFGA tuple store is not configured")
	}
	if objectType == "" {
		return nil, errors.New("object type is required")
	}
	var (
		tuples []relationships.Tuple
		token  *string
		seen         = map[string]struct{}{}
		page   int32 = 100
	)
	for {
		options := client.ClientReadOptions{PageSize: &page}
		if token != nil {
			options.ContinuationToken = token
		}
		response, err := s.client.Read(ctx).
			Options(options).
			Body(client.ClientReadRequest{}).
			Execute()
		if err != nil {
			return nil, fmt.Errorf("read OpenFGA tuples: %w", err)
		}
		if response == nil {
			return nil, errors.New("OpenFGA returned no read response")
		}
		prefix := objectType + ":"
		for _, tuple := range response.GetTuples() {
			key := tuple.GetKey()
			if !strings.HasPrefix(key.GetObject(), prefix) {
				continue
			}
			converted := relationships.Tuple{User: key.GetUser(), Relation: key.GetRelation(), Object: key.GetObject()}
			if _, dup := seen[converted.Key()]; dup {
				continue
			}
			seen[converted.Key()] = struct{}{}
			tuples = append(tuples, converted)
		}
		next := response.GetContinuationToken()
		if next == "" {
			return tuples, nil
		}
		if _, repeated := seen["token:"+next]; repeated {
			return nil, errors.New("OpenFGA repeated a continuation token")
		}
		seen["token:"+next] = struct{}{}
		token = &next
	}
}

// Apply deletes revoked tuples before writing new ones, in bounded batches, so
// a partial failure never leaves a revoked grant in place while adding others.
func (s *TupleStore) Apply(ctx context.Context, change relationships.ChangeSet) error {
	if s == nil || s.client == nil {
		return errors.New("OpenFGA tuple store is not configured")
	}
	if len(change.Deletes) == 0 && len(change.Writes) == 0 {
		return nil
	}
	for _, batch := range chunk(change.Deletes, s.batch) {
		deletes := make([]client.ClientTupleKeyWithoutCondition, 0, len(batch))
		for _, tuple := range batch {
			deletes = append(deletes, client.ClientTupleKeyWithoutCondition{
				User: tuple.User, Relation: tuple.Relation, Object: tuple.Object,
			})
		}
		if err := s.write(ctx, client.ClientWriteRequest{Deletes: deletes}); err != nil {
			return err
		}
	}
	for _, batch := range chunk(change.Writes, s.batch) {
		writes := make([]client.ClientTupleKey, 0, len(batch))
		for _, tuple := range batch {
			writes = append(writes, client.ClientTupleKey{
				User: tuple.User, Relation: tuple.Relation, Object: tuple.Object,
			})
		}
		if err := s.write(ctx, client.ClientWriteRequest{Writes: writes}); err != nil {
			return err
		}
	}
	return nil
}

func (s *TupleStore) write(ctx context.Context, request client.ClientWriteRequest) error {
	modelID, storeID := s.modelID, s.storeID
	_, err := s.client.Write(ctx).
		Options(client.ClientWriteOptions{AuthorizationModelId: &modelID, StoreId: &storeID}).
		Body(request).
		Execute()
	if err != nil {
		return fmt.Errorf("write OpenFGA tuples: %w", err)
	}
	return nil
}

func chunk(tuples []relationships.Tuple, size int) [][]relationships.Tuple {
	if size <= 0 {
		size = defaultTupleBatch
	}
	var batches [][]relationships.Tuple
	for len(tuples) > 0 {
		take := size
		if take > len(tuples) {
			take = len(tuples)
		}
		batches = append(batches, tuples[:take])
		tuples = tuples[take:]
	}
	return batches
}
