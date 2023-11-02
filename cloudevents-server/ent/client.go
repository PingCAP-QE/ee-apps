// Code generated by ent, DO NOT EDIT.

package ent

import (
	"context"
	"errors"
	"fmt"
	"log"
	"reflect"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent/migrate"

	"entgo.io/ent"
	"entgo.io/ent/dialect"
	"entgo.io/ent/dialect/sql"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent/problemcaserun"
)

// Client is the client that holds all ent builders.
type Client struct {
	config
	// Schema is the client for creating, migrating and dropping schema.
	Schema *migrate.Schema
	// ProblemCaseRun is the client for interacting with the ProblemCaseRun builders.
	ProblemCaseRun *ProblemCaseRunClient
}

// NewClient creates a new client configured with the given options.
func NewClient(opts ...Option) *Client {
	cfg := config{log: log.Println, hooks: &hooks{}, inters: &inters{}}
	cfg.options(opts...)
	client := &Client{config: cfg}
	client.init()
	return client
}

func (c *Client) init() {
	c.Schema = migrate.NewSchema(c.driver)
	c.ProblemCaseRun = NewProblemCaseRunClient(c.config)
}

type (
	// config is the configuration for the client and its builder.
	config struct {
		// driver used for executing database requests.
		driver dialect.Driver
		// debug enable a debug logging.
		debug bool
		// log used for logging on debug mode.
		log func(...any)
		// hooks to execute on mutations.
		hooks *hooks
		// interceptors to execute on queries.
		inters *inters
	}
	// Option function to configure the client.
	Option func(*config)
)

// options applies the options on the config object.
func (c *config) options(opts ...Option) {
	for _, opt := range opts {
		opt(c)
	}
	if c.debug {
		c.driver = dialect.Debug(c.driver, c.log)
	}
}

// Debug enables debug logging on the ent.Driver.
func Debug() Option {
	return func(c *config) {
		c.debug = true
	}
}

// Log sets the logging function for debug mode.
func Log(fn func(...any)) Option {
	return func(c *config) {
		c.log = fn
	}
}

// Driver configures the client driver.
func Driver(driver dialect.Driver) Option {
	return func(c *config) {
		c.driver = driver
	}
}

// Open opens a database/sql.DB specified by the driver name and
// the data source name, and returns a new client attached to it.
// Optional parameters can be added for configuring the client.
func Open(driverName, dataSourceName string, options ...Option) (*Client, error) {
	switch driverName {
	case dialect.MySQL, dialect.Postgres, dialect.SQLite:
		drv, err := sql.Open(driverName, dataSourceName)
		if err != nil {
			return nil, err
		}
		return NewClient(append(options, Driver(drv))...), nil
	default:
		return nil, fmt.Errorf("unsupported driver: %q", driverName)
	}
}

// ErrTxStarted is returned when trying to start a new transaction from a transactional client.
var ErrTxStarted = errors.New("ent: cannot start a transaction within a transaction")

// Tx returns a new transactional client. The provided context
// is used until the transaction is committed or rolled back.
func (c *Client) Tx(ctx context.Context) (*Tx, error) {
	if _, ok := c.driver.(*txDriver); ok {
		return nil, ErrTxStarted
	}
	tx, err := newTx(ctx, c.driver)
	if err != nil {
		return nil, fmt.Errorf("ent: starting a transaction: %w", err)
	}
	cfg := c.config
	cfg.driver = tx
	return &Tx{
		ctx:            ctx,
		config:         cfg,
		ProblemCaseRun: NewProblemCaseRunClient(cfg),
	}, nil
}

// BeginTx returns a transactional client with specified options.
func (c *Client) BeginTx(ctx context.Context, opts *sql.TxOptions) (*Tx, error) {
	if _, ok := c.driver.(*txDriver); ok {
		return nil, errors.New("ent: cannot start a transaction within a transaction")
	}
	tx, err := c.driver.(interface {
		BeginTx(context.Context, *sql.TxOptions) (dialect.Tx, error)
	}).BeginTx(ctx, opts)
	if err != nil {
		return nil, fmt.Errorf("ent: starting a transaction: %w", err)
	}
	cfg := c.config
	cfg.driver = &txDriver{tx: tx, drv: c.driver}
	return &Tx{
		ctx:            ctx,
		config:         cfg,
		ProblemCaseRun: NewProblemCaseRunClient(cfg),
	}, nil
}

// Debug returns a new debug-client. It's used to get verbose logging on specific operations.
//
//	client.Debug().
//		ProblemCaseRun.
//		Query().
//		Count(ctx)
func (c *Client) Debug() *Client {
	if c.debug {
		return c
	}
	cfg := c.config
	cfg.driver = dialect.Debug(c.driver, c.log)
	client := &Client{config: cfg}
	client.init()
	return client
}

// Close closes the database connection and prevents new queries from starting.
func (c *Client) Close() error {
	return c.driver.Close()
}

// Use adds the mutation hooks to all the entity clients.
// In order to add hooks to a specific client, call: `client.Node.Use(...)`.
func (c *Client) Use(hooks ...Hook) {
	c.ProblemCaseRun.Use(hooks...)
}

// Intercept adds the query interceptors to all the entity clients.
// In order to add interceptors to a specific client, call: `client.Node.Intercept(...)`.
func (c *Client) Intercept(interceptors ...Interceptor) {
	c.ProblemCaseRun.Intercept(interceptors...)
}

// Mutate implements the ent.Mutator interface.
func (c *Client) Mutate(ctx context.Context, m Mutation) (Value, error) {
	switch m := m.(type) {
	case *ProblemCaseRunMutation:
		return c.ProblemCaseRun.mutate(ctx, m)
	default:
		return nil, fmt.Errorf("ent: unknown mutation type %T", m)
	}
}

// ProblemCaseRunClient is a client for the ProblemCaseRun schema.
type ProblemCaseRunClient struct {
	config
}

// NewProblemCaseRunClient returns a client for the ProblemCaseRun from the given config.
func NewProblemCaseRunClient(c config) *ProblemCaseRunClient {
	return &ProblemCaseRunClient{config: c}
}

// Use adds a list of mutation hooks to the hooks stack.
// A call to `Use(f, g, h)` equals to `problemcaserun.Hooks(f(g(h())))`.
func (c *ProblemCaseRunClient) Use(hooks ...Hook) {
	c.hooks.ProblemCaseRun = append(c.hooks.ProblemCaseRun, hooks...)
}

// Intercept adds a list of query interceptors to the interceptors stack.
// A call to `Intercept(f, g, h)` equals to `problemcaserun.Intercept(f(g(h())))`.
func (c *ProblemCaseRunClient) Intercept(interceptors ...Interceptor) {
	c.inters.ProblemCaseRun = append(c.inters.ProblemCaseRun, interceptors...)
}

// Create returns a builder for creating a ProblemCaseRun entity.
func (c *ProblemCaseRunClient) Create() *ProblemCaseRunCreate {
	mutation := newProblemCaseRunMutation(c.config, OpCreate)
	return &ProblemCaseRunCreate{config: c.config, hooks: c.Hooks(), mutation: mutation}
}

// CreateBulk returns a builder for creating a bulk of ProblemCaseRun entities.
func (c *ProblemCaseRunClient) CreateBulk(builders ...*ProblemCaseRunCreate) *ProblemCaseRunCreateBulk {
	return &ProblemCaseRunCreateBulk{config: c.config, builders: builders}
}

// MapCreateBulk creates a bulk creation builder from the given slice. For each item in the slice, the function creates
// a builder and applies setFunc on it.
func (c *ProblemCaseRunClient) MapCreateBulk(slice any, setFunc func(*ProblemCaseRunCreate, int)) *ProblemCaseRunCreateBulk {
	rv := reflect.ValueOf(slice)
	if rv.Kind() != reflect.Slice {
		return &ProblemCaseRunCreateBulk{err: fmt.Errorf("calling to ProblemCaseRunClient.MapCreateBulk with wrong type %T, need slice", slice)}
	}
	builders := make([]*ProblemCaseRunCreate, rv.Len())
	for i := 0; i < rv.Len(); i++ {
		builders[i] = c.Create()
		setFunc(builders[i], i)
	}
	return &ProblemCaseRunCreateBulk{config: c.config, builders: builders}
}

// Update returns an update builder for ProblemCaseRun.
func (c *ProblemCaseRunClient) Update() *ProblemCaseRunUpdate {
	mutation := newProblemCaseRunMutation(c.config, OpUpdate)
	return &ProblemCaseRunUpdate{config: c.config, hooks: c.Hooks(), mutation: mutation}
}

// UpdateOne returns an update builder for the given entity.
func (c *ProblemCaseRunClient) UpdateOne(pcr *ProblemCaseRun) *ProblemCaseRunUpdateOne {
	mutation := newProblemCaseRunMutation(c.config, OpUpdateOne, withProblemCaseRun(pcr))
	return &ProblemCaseRunUpdateOne{config: c.config, hooks: c.Hooks(), mutation: mutation}
}

// UpdateOneID returns an update builder for the given id.
func (c *ProblemCaseRunClient) UpdateOneID(id int) *ProblemCaseRunUpdateOne {
	mutation := newProblemCaseRunMutation(c.config, OpUpdateOne, withProblemCaseRunID(id))
	return &ProblemCaseRunUpdateOne{config: c.config, hooks: c.Hooks(), mutation: mutation}
}

// Delete returns a delete builder for ProblemCaseRun.
func (c *ProblemCaseRunClient) Delete() *ProblemCaseRunDelete {
	mutation := newProblemCaseRunMutation(c.config, OpDelete)
	return &ProblemCaseRunDelete{config: c.config, hooks: c.Hooks(), mutation: mutation}
}

// DeleteOne returns a builder for deleting the given entity.
func (c *ProblemCaseRunClient) DeleteOne(pcr *ProblemCaseRun) *ProblemCaseRunDeleteOne {
	return c.DeleteOneID(pcr.ID)
}

// DeleteOneID returns a builder for deleting the given entity by its id.
func (c *ProblemCaseRunClient) DeleteOneID(id int) *ProblemCaseRunDeleteOne {
	builder := c.Delete().Where(problemcaserun.ID(id))
	builder.mutation.id = &id
	builder.mutation.op = OpDeleteOne
	return &ProblemCaseRunDeleteOne{builder}
}

// Query returns a query builder for ProblemCaseRun.
func (c *ProblemCaseRunClient) Query() *ProblemCaseRunQuery {
	return &ProblemCaseRunQuery{
		config: c.config,
		ctx:    &QueryContext{Type: TypeProblemCaseRun},
		inters: c.Interceptors(),
	}
}

// Get returns a ProblemCaseRun entity by its id.
func (c *ProblemCaseRunClient) Get(ctx context.Context, id int) (*ProblemCaseRun, error) {
	return c.Query().Where(problemcaserun.ID(id)).Only(ctx)
}

// GetX is like Get, but panics if an error occurs.
func (c *ProblemCaseRunClient) GetX(ctx context.Context, id int) *ProblemCaseRun {
	obj, err := c.Get(ctx, id)
	if err != nil {
		panic(err)
	}
	return obj
}

// Hooks returns the client hooks.
func (c *ProblemCaseRunClient) Hooks() []Hook {
	return c.hooks.ProblemCaseRun
}

// Interceptors returns the client interceptors.
func (c *ProblemCaseRunClient) Interceptors() []Interceptor {
	return c.inters.ProblemCaseRun
}

func (c *ProblemCaseRunClient) mutate(ctx context.Context, m *ProblemCaseRunMutation) (Value, error) {
	switch m.Op() {
	case OpCreate:
		return (&ProblemCaseRunCreate{config: c.config, hooks: c.Hooks(), mutation: m}).Save(ctx)
	case OpUpdate:
		return (&ProblemCaseRunUpdate{config: c.config, hooks: c.Hooks(), mutation: m}).Save(ctx)
	case OpUpdateOne:
		return (&ProblemCaseRunUpdateOne{config: c.config, hooks: c.Hooks(), mutation: m}).Save(ctx)
	case OpDelete, OpDeleteOne:
		return (&ProblemCaseRunDelete{config: c.config, hooks: c.Hooks(), mutation: m}).Exec(ctx)
	default:
		return nil, fmt.Errorf("ent: unknown ProblemCaseRun mutation op: %q", m.Op())
	}
}

// hooks and interceptors per client, for fast access.
type (
	hooks struct {
		ProblemCaseRun []ent.Hook
	}
	inters struct {
		ProblemCaseRun []ent.Interceptor
	}
)
