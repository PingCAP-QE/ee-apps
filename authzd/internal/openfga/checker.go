package openfga

import (
	"context"
	"errors"
	"fmt"
	"net/url"

	fga "github.com/openfga/go-sdk"
	"github.com/openfga/go-sdk/client"
	"github.com/openfga/go-sdk/credentials"
	"github.com/openfga/go-sdk/telemetry"
)

type Config struct {
	APIURL   string
	StoreID  string
	ModelID  string
	APIToken string
}

type Checker struct {
	client  *client.OpenFgaClient
	storeID string
	modelID string
}

func NewChecker(cfg Config) (*Checker, error) {
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
		ApiUrl: cfg.APIURL, StoreId: cfg.StoreID, AuthorizationModelId: cfg.ModelID,
		Credentials: creds, RetryParams: &fga.RetryParams{MaxRetry: 2, MinWaitInMs: 50},
		Telemetry: &telemetry.Configuration{},
	})
	if err != nil {
		return nil, fmt.Errorf("create OpenFGA client: %w", err)
	}
	return &Checker{client: fgaClient, storeID: cfg.StoreID, modelID: cfg.ModelID}, nil
}

func (c *Checker) Check(ctx context.Context, user, object, relation string, higherConsistency bool) (bool, error) {
	if c == nil || c.client == nil {
		return false, errors.New("OpenFGA client is not configured")
	}
	consistency := fga.CONSISTENCYPREFERENCE_MINIMIZE_LATENCY
	if higherConsistency {
		consistency = fga.CONSISTENCYPREFERENCE_HIGHER_CONSISTENCY
	}
	modelID, storeID := c.modelID, c.storeID
	response, err := c.client.Check(ctx).
		Options(client.ClientCheckOptions{AuthorizationModelId: &modelID, StoreId: &storeID, Consistency: &consistency}).
		Body(client.ClientCheckRequest{User: user, Object: object, Relation: relation}).
		Execute()
	if err != nil {
		return false, err
	}
	if response == nil || response.Allowed == nil {
		return false, errors.New("OpenFGA returned no decision")
	}
	return *response.Allowed, nil
}

func (c *Checker) ModelID() string {
	if c == nil {
		return ""
	}
	return c.modelID
}
