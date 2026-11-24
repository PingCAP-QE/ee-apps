"""Portable utilization tables. Creating them is an explicit migration/test operation."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()
ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


def _identity():
    return [
        Column("vendor", String(16), nullable=False),
        Column("account_id", String(32), nullable=False),
    ]


daily = Table(
    "cost_utilization_daily",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    *_identity(),
    Column("usage_date", Date, nullable=False),
    Column("week_start", Date, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("scope", String(24), nullable=False),
    Column("resource_key", String(64), nullable=False),
    Column("resource_class", String(64), nullable=False),
    Column("resource_spec", String(255), nullable=False, default=""),
    Column("repo", String(255), nullable=False, default="unknown_repo"),
    Column("job_name", String(512), nullable=False, default="unknown_job"),
    Column("namespace", String(128), nullable=False, default=""),
    Column("region", String(64), nullable=False, default=""),
    Column("metrics_json", JSON, nullable=False),
    Column("cutoff", DateTime, nullable=False),
    Column("evidence_cutoff", DateTime),
    Column("status", String(24), nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint(
        "vendor", "account_id", "usage_date", "scope", "resource_key", name="uq_utilization_daily"
    ),
)
Index("ix_utilization_daily_expiry", daily.c.expires_at, daily.c.id)
Index(
    "ix_utilization_daily_week",
    daily.c.vendor,
    daily.c.account_id,
    daily.c.week_start,
    daily.c.scope,
    daily.c.resource_class,
)

weekly = Table(
    "cost_utilization_weekly",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    *_identity(),
    Column("week_start", Date, nullable=False),
    Column("resource_class", String(64), nullable=False),
    Column("metric", String(32), nullable=False),
    Column("contract_json", JSON, nullable=False),
    Column("values_json", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("quality_checkpoint_at", DateTime),
    Column("finalized_at", DateTime),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint(
        "vendor",
        "account_id",
        "week_start",
        "resource_class",
        "metric",
        name="uq_utilization_weekly",
    ),
)

observations = Table(
    "cost_utilization_resource_observation",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    *_identity(),
    Column("week_start", Date, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("resource_type", String(8), nullable=False),
    Column("resource_id", String(255), nullable=False),
    Column("cluster", String(128), nullable=False, default=""),
    Column("namespace", String(128), nullable=False, default=""),
    Column("name", String(255), nullable=False, default=""),
    Column("provider_id", String(255), nullable=False, default=""),
    Column("observed_from", DateTime, nullable=False),
    Column("observed_until", DateTime),
    Column("last_seen", DateTime, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("attributes_json", JSON, nullable=False),
    UniqueConstraint(
        "vendor",
        "account_id",
        "week_start",
        "resource_type",
        "resource_id",
        "observed_from",
        name="uq_utilization_observation",
    ),
)
Index("ix_utilization_observation_expiry", observations.c.expires_at, observations.c.id)
Index(
    "ix_utilization_observation_interval",
    observations.c.vendor,
    observations.c.account_id,
    observations.c.week_start,
    observations.c.resource_type,
)

OBSERVATION_ATTRIBUTES = frozenset(
    {
        "repo",
        "job_name",
        "node_name",
        "cpu_request",
        "memory_request",
        "cpu_limit",
        "memory_limit",
        "cpu_capacity",
        "memory_capacity",
        "allocatable_cpu",
        "allocatable_memory",
        "instance_type",
        "resource_class",
        "region",
        "created_at",
        "ended_at",
        "metadata_confidence",
    }
)
