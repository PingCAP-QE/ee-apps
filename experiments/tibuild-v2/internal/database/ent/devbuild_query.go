// Code generated by ent, DO NOT EDIT.

package ent

import (
	"context"
	"fmt"
	"math"

	"entgo.io/ent"
	"entgo.io/ent/dialect/sql"
	"entgo.io/ent/dialect/sql/sqlgraph"
	"entgo.io/ent/schema/field"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/devbuild"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent/predicate"
)

// DevBuildQuery is the builder for querying DevBuild entities.
type DevBuildQuery struct {
	config
	ctx        *QueryContext
	order      []devbuild.OrderOption
	inters     []Interceptor
	predicates []predicate.DevBuild
	// intermediate query (i.e. traversal path).
	sql  *sql.Selector
	path func(context.Context) (*sql.Selector, error)
}

// Where adds a new predicate for the DevBuildQuery builder.
func (dbq *DevBuildQuery) Where(ps ...predicate.DevBuild) *DevBuildQuery {
	dbq.predicates = append(dbq.predicates, ps...)
	return dbq
}

// Limit the number of records to be returned by this query.
func (dbq *DevBuildQuery) Limit(limit int) *DevBuildQuery {
	dbq.ctx.Limit = &limit
	return dbq
}

// Offset to start from.
func (dbq *DevBuildQuery) Offset(offset int) *DevBuildQuery {
	dbq.ctx.Offset = &offset
	return dbq
}

// Unique configures the query builder to filter duplicate records on query.
// By default, unique is set to true, and can be disabled using this method.
func (dbq *DevBuildQuery) Unique(unique bool) *DevBuildQuery {
	dbq.ctx.Unique = &unique
	return dbq
}

// Order specifies how the records should be ordered.
func (dbq *DevBuildQuery) Order(o ...devbuild.OrderOption) *DevBuildQuery {
	dbq.order = append(dbq.order, o...)
	return dbq
}

// First returns the first DevBuild entity from the query.
// Returns a *NotFoundError when no DevBuild was found.
func (dbq *DevBuildQuery) First(ctx context.Context) (*DevBuild, error) {
	nodes, err := dbq.Limit(1).All(setContextOp(ctx, dbq.ctx, ent.OpQueryFirst))
	if err != nil {
		return nil, err
	}
	if len(nodes) == 0 {
		return nil, &NotFoundError{devbuild.Label}
	}
	return nodes[0], nil
}

// FirstX is like First, but panics if an error occurs.
func (dbq *DevBuildQuery) FirstX(ctx context.Context) *DevBuild {
	node, err := dbq.First(ctx)
	if err != nil && !IsNotFound(err) {
		panic(err)
	}
	return node
}

// FirstID returns the first DevBuild ID from the query.
// Returns a *NotFoundError when no DevBuild ID was found.
func (dbq *DevBuildQuery) FirstID(ctx context.Context) (id int, err error) {
	var ids []int
	if ids, err = dbq.Limit(1).IDs(setContextOp(ctx, dbq.ctx, ent.OpQueryFirstID)); err != nil {
		return
	}
	if len(ids) == 0 {
		err = &NotFoundError{devbuild.Label}
		return
	}
	return ids[0], nil
}

// FirstIDX is like FirstID, but panics if an error occurs.
func (dbq *DevBuildQuery) FirstIDX(ctx context.Context) int {
	id, err := dbq.FirstID(ctx)
	if err != nil && !IsNotFound(err) {
		panic(err)
	}
	return id
}

// Only returns a single DevBuild entity found by the query, ensuring it only returns one.
// Returns a *NotSingularError when more than one DevBuild entity is found.
// Returns a *NotFoundError when no DevBuild entities are found.
func (dbq *DevBuildQuery) Only(ctx context.Context) (*DevBuild, error) {
	nodes, err := dbq.Limit(2).All(setContextOp(ctx, dbq.ctx, ent.OpQueryOnly))
	if err != nil {
		return nil, err
	}
	switch len(nodes) {
	case 1:
		return nodes[0], nil
	case 0:
		return nil, &NotFoundError{devbuild.Label}
	default:
		return nil, &NotSingularError{devbuild.Label}
	}
}

// OnlyX is like Only, but panics if an error occurs.
func (dbq *DevBuildQuery) OnlyX(ctx context.Context) *DevBuild {
	node, err := dbq.Only(ctx)
	if err != nil {
		panic(err)
	}
	return node
}

// OnlyID is like Only, but returns the only DevBuild ID in the query.
// Returns a *NotSingularError when more than one DevBuild ID is found.
// Returns a *NotFoundError when no entities are found.
func (dbq *DevBuildQuery) OnlyID(ctx context.Context) (id int, err error) {
	var ids []int
	if ids, err = dbq.Limit(2).IDs(setContextOp(ctx, dbq.ctx, ent.OpQueryOnlyID)); err != nil {
		return
	}
	switch len(ids) {
	case 1:
		id = ids[0]
	case 0:
		err = &NotFoundError{devbuild.Label}
	default:
		err = &NotSingularError{devbuild.Label}
	}
	return
}

// OnlyIDX is like OnlyID, but panics if an error occurs.
func (dbq *DevBuildQuery) OnlyIDX(ctx context.Context) int {
	id, err := dbq.OnlyID(ctx)
	if err != nil {
		panic(err)
	}
	return id
}

// All executes the query and returns a list of DevBuilds.
func (dbq *DevBuildQuery) All(ctx context.Context) ([]*DevBuild, error) {
	ctx = setContextOp(ctx, dbq.ctx, ent.OpQueryAll)
	if err := dbq.prepareQuery(ctx); err != nil {
		return nil, err
	}
	qr := querierAll[[]*DevBuild, *DevBuildQuery]()
	return withInterceptors[[]*DevBuild](ctx, dbq, qr, dbq.inters)
}

// AllX is like All, but panics if an error occurs.
func (dbq *DevBuildQuery) AllX(ctx context.Context) []*DevBuild {
	nodes, err := dbq.All(ctx)
	if err != nil {
		panic(err)
	}
	return nodes
}

// IDs executes the query and returns a list of DevBuild IDs.
func (dbq *DevBuildQuery) IDs(ctx context.Context) (ids []int, err error) {
	if dbq.ctx.Unique == nil && dbq.path != nil {
		dbq.Unique(true)
	}
	ctx = setContextOp(ctx, dbq.ctx, ent.OpQueryIDs)
	if err = dbq.Select(devbuild.FieldID).Scan(ctx, &ids); err != nil {
		return nil, err
	}
	return ids, nil
}

// IDsX is like IDs, but panics if an error occurs.
func (dbq *DevBuildQuery) IDsX(ctx context.Context) []int {
	ids, err := dbq.IDs(ctx)
	if err != nil {
		panic(err)
	}
	return ids
}

// Count returns the count of the given query.
func (dbq *DevBuildQuery) Count(ctx context.Context) (int, error) {
	ctx = setContextOp(ctx, dbq.ctx, ent.OpQueryCount)
	if err := dbq.prepareQuery(ctx); err != nil {
		return 0, err
	}
	return withInterceptors[int](ctx, dbq, querierCount[*DevBuildQuery](), dbq.inters)
}

// CountX is like Count, but panics if an error occurs.
func (dbq *DevBuildQuery) CountX(ctx context.Context) int {
	count, err := dbq.Count(ctx)
	if err != nil {
		panic(err)
	}
	return count
}

// Exist returns true if the query has elements in the graph.
func (dbq *DevBuildQuery) Exist(ctx context.Context) (bool, error) {
	ctx = setContextOp(ctx, dbq.ctx, ent.OpQueryExist)
	switch _, err := dbq.FirstID(ctx); {
	case IsNotFound(err):
		return false, nil
	case err != nil:
		return false, fmt.Errorf("ent: check existence: %w", err)
	default:
		return true, nil
	}
}

// ExistX is like Exist, but panics if an error occurs.
func (dbq *DevBuildQuery) ExistX(ctx context.Context) bool {
	exist, err := dbq.Exist(ctx)
	if err != nil {
		panic(err)
	}
	return exist
}

// Clone returns a duplicate of the DevBuildQuery builder, including all associated steps. It can be
// used to prepare common query builders and use them differently after the clone is made.
func (dbq *DevBuildQuery) Clone() *DevBuildQuery {
	if dbq == nil {
		return nil
	}
	return &DevBuildQuery{
		config:     dbq.config,
		ctx:        dbq.ctx.Clone(),
		order:      append([]devbuild.OrderOption{}, dbq.order...),
		inters:     append([]Interceptor{}, dbq.inters...),
		predicates: append([]predicate.DevBuild{}, dbq.predicates...),
		// clone intermediate query.
		sql:  dbq.sql.Clone(),
		path: dbq.path,
	}
}

// GroupBy is used to group vertices by one or more fields/columns.
// It is often used with aggregate functions, like: count, max, mean, min, sum.
//
// Example:
//
//	var v []struct {
//		CreatedBy string `json:"created_by,omitempty"`
//		Count int `json:"count,omitempty"`
//	}
//
//	client.DevBuild.Query().
//		GroupBy(devbuild.FieldCreatedBy).
//		Aggregate(ent.Count()).
//		Scan(ctx, &v)
func (dbq *DevBuildQuery) GroupBy(field string, fields ...string) *DevBuildGroupBy {
	dbq.ctx.Fields = append([]string{field}, fields...)
	grbuild := &DevBuildGroupBy{build: dbq}
	grbuild.flds = &dbq.ctx.Fields
	grbuild.label = devbuild.Label
	grbuild.scan = grbuild.Scan
	return grbuild
}

// Select allows the selection one or more fields/columns for the given query,
// instead of selecting all fields in the entity.
//
// Example:
//
//	var v []struct {
//		CreatedBy string `json:"created_by,omitempty"`
//	}
//
//	client.DevBuild.Query().
//		Select(devbuild.FieldCreatedBy).
//		Scan(ctx, &v)
func (dbq *DevBuildQuery) Select(fields ...string) *DevBuildSelect {
	dbq.ctx.Fields = append(dbq.ctx.Fields, fields...)
	sbuild := &DevBuildSelect{DevBuildQuery: dbq}
	sbuild.label = devbuild.Label
	sbuild.flds, sbuild.scan = &dbq.ctx.Fields, sbuild.Scan
	return sbuild
}

// Aggregate returns a DevBuildSelect configured with the given aggregations.
func (dbq *DevBuildQuery) Aggregate(fns ...AggregateFunc) *DevBuildSelect {
	return dbq.Select().Aggregate(fns...)
}

func (dbq *DevBuildQuery) prepareQuery(ctx context.Context) error {
	for _, inter := range dbq.inters {
		if inter == nil {
			return fmt.Errorf("ent: uninitialized interceptor (forgotten import ent/runtime?)")
		}
		if trv, ok := inter.(Traverser); ok {
			if err := trv.Traverse(ctx, dbq); err != nil {
				return err
			}
		}
	}
	for _, f := range dbq.ctx.Fields {
		if !devbuild.ValidColumn(f) {
			return &ValidationError{Name: f, err: fmt.Errorf("ent: invalid field %q for query", f)}
		}
	}
	if dbq.path != nil {
		prev, err := dbq.path(ctx)
		if err != nil {
			return err
		}
		dbq.sql = prev
	}
	return nil
}

func (dbq *DevBuildQuery) sqlAll(ctx context.Context, hooks ...queryHook) ([]*DevBuild, error) {
	var (
		nodes = []*DevBuild{}
		_spec = dbq.querySpec()
	)
	_spec.ScanValues = func(columns []string) ([]any, error) {
		return (*DevBuild).scanValues(nil, columns)
	}
	_spec.Assign = func(columns []string, values []any) error {
		node := &DevBuild{config: dbq.config}
		nodes = append(nodes, node)
		return node.assignValues(columns, values)
	}
	for i := range hooks {
		hooks[i](ctx, _spec)
	}
	if err := sqlgraph.QueryNodes(ctx, dbq.driver, _spec); err != nil {
		return nil, err
	}
	if len(nodes) == 0 {
		return nodes, nil
	}
	return nodes, nil
}

func (dbq *DevBuildQuery) sqlCount(ctx context.Context) (int, error) {
	_spec := dbq.querySpec()
	_spec.Node.Columns = dbq.ctx.Fields
	if len(dbq.ctx.Fields) > 0 {
		_spec.Unique = dbq.ctx.Unique != nil && *dbq.ctx.Unique
	}
	return sqlgraph.CountNodes(ctx, dbq.driver, _spec)
}

func (dbq *DevBuildQuery) querySpec() *sqlgraph.QuerySpec {
	_spec := sqlgraph.NewQuerySpec(devbuild.Table, devbuild.Columns, sqlgraph.NewFieldSpec(devbuild.FieldID, field.TypeInt))
	_spec.From = dbq.sql
	if unique := dbq.ctx.Unique; unique != nil {
		_spec.Unique = *unique
	} else if dbq.path != nil {
		_spec.Unique = true
	}
	if fields := dbq.ctx.Fields; len(fields) > 0 {
		_spec.Node.Columns = make([]string, 0, len(fields))
		_spec.Node.Columns = append(_spec.Node.Columns, devbuild.FieldID)
		for i := range fields {
			if fields[i] != devbuild.FieldID {
				_spec.Node.Columns = append(_spec.Node.Columns, fields[i])
			}
		}
	}
	if ps := dbq.predicates; len(ps) > 0 {
		_spec.Predicate = func(selector *sql.Selector) {
			for i := range ps {
				ps[i](selector)
			}
		}
	}
	if limit := dbq.ctx.Limit; limit != nil {
		_spec.Limit = *limit
	}
	if offset := dbq.ctx.Offset; offset != nil {
		_spec.Offset = *offset
	}
	if ps := dbq.order; len(ps) > 0 {
		_spec.Order = func(selector *sql.Selector) {
			for i := range ps {
				ps[i](selector)
			}
		}
	}
	return _spec
}

func (dbq *DevBuildQuery) sqlQuery(ctx context.Context) *sql.Selector {
	builder := sql.Dialect(dbq.driver.Dialect())
	t1 := builder.Table(devbuild.Table)
	columns := dbq.ctx.Fields
	if len(columns) == 0 {
		columns = devbuild.Columns
	}
	selector := builder.Select(t1.Columns(columns...)...).From(t1)
	if dbq.sql != nil {
		selector = dbq.sql
		selector.Select(selector.Columns(columns...)...)
	}
	if dbq.ctx.Unique != nil && *dbq.ctx.Unique {
		selector.Distinct()
	}
	for _, p := range dbq.predicates {
		p(selector)
	}
	for _, p := range dbq.order {
		p(selector)
	}
	if offset := dbq.ctx.Offset; offset != nil {
		// limit is mandatory for offset clause. We start
		// with default value, and override it below if needed.
		selector.Offset(*offset).Limit(math.MaxInt32)
	}
	if limit := dbq.ctx.Limit; limit != nil {
		selector.Limit(*limit)
	}
	return selector
}

// DevBuildGroupBy is the group-by builder for DevBuild entities.
type DevBuildGroupBy struct {
	selector
	build *DevBuildQuery
}

// Aggregate adds the given aggregation functions to the group-by query.
func (dbgb *DevBuildGroupBy) Aggregate(fns ...AggregateFunc) *DevBuildGroupBy {
	dbgb.fns = append(dbgb.fns, fns...)
	return dbgb
}

// Scan applies the selector query and scans the result into the given value.
func (dbgb *DevBuildGroupBy) Scan(ctx context.Context, v any) error {
	ctx = setContextOp(ctx, dbgb.build.ctx, ent.OpQueryGroupBy)
	if err := dbgb.build.prepareQuery(ctx); err != nil {
		return err
	}
	return scanWithInterceptors[*DevBuildQuery, *DevBuildGroupBy](ctx, dbgb.build, dbgb, dbgb.build.inters, v)
}

func (dbgb *DevBuildGroupBy) sqlScan(ctx context.Context, root *DevBuildQuery, v any) error {
	selector := root.sqlQuery(ctx).Select()
	aggregation := make([]string, 0, len(dbgb.fns))
	for _, fn := range dbgb.fns {
		aggregation = append(aggregation, fn(selector))
	}
	if len(selector.SelectedColumns()) == 0 {
		columns := make([]string, 0, len(*dbgb.flds)+len(dbgb.fns))
		for _, f := range *dbgb.flds {
			columns = append(columns, selector.C(f))
		}
		columns = append(columns, aggregation...)
		selector.Select(columns...)
	}
	selector.GroupBy(selector.Columns(*dbgb.flds...)...)
	if err := selector.Err(); err != nil {
		return err
	}
	rows := &sql.Rows{}
	query, args := selector.Query()
	if err := dbgb.build.driver.Query(ctx, query, args, rows); err != nil {
		return err
	}
	defer rows.Close()
	return sql.ScanSlice(rows, v)
}

// DevBuildSelect is the builder for selecting fields of DevBuild entities.
type DevBuildSelect struct {
	*DevBuildQuery
	selector
}

// Aggregate adds the given aggregation functions to the selector query.
func (dbs *DevBuildSelect) Aggregate(fns ...AggregateFunc) *DevBuildSelect {
	dbs.fns = append(dbs.fns, fns...)
	return dbs
}

// Scan applies the selector query and scans the result into the given value.
func (dbs *DevBuildSelect) Scan(ctx context.Context, v any) error {
	ctx = setContextOp(ctx, dbs.ctx, ent.OpQuerySelect)
	if err := dbs.prepareQuery(ctx); err != nil {
		return err
	}
	return scanWithInterceptors[*DevBuildQuery, *DevBuildSelect](ctx, dbs.DevBuildQuery, dbs, dbs.inters, v)
}

func (dbs *DevBuildSelect) sqlScan(ctx context.Context, root *DevBuildQuery, v any) error {
	selector := root.sqlQuery(ctx)
	aggregation := make([]string, 0, len(dbs.fns))
	for _, fn := range dbs.fns {
		aggregation = append(aggregation, fn(selector))
	}
	switch n := len(*dbs.selector.flds); {
	case n == 0 && len(aggregation) > 0:
		selector.Select(aggregation...)
	case n != 0 && len(aggregation) > 0:
		selector.AppendSelect(aggregation...)
	}
	rows := &sql.Rows{}
	query, args := selector.Query()
	if err := dbs.driver.Query(ctx, query, args, rows); err != nil {
		return err
	}
	defer rows.Close()
	return sql.ScanSlice(rows, v)
}
