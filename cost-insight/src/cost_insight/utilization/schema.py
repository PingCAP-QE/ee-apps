"""V2 owned tables. Creation is an explicit offline migration/operator action."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Date,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy import (
    DateTime as SQLDateTime,
)
from sqlalchemy import (
    Double as Float,
)
from sqlalchemy.dialects.mysql import DATETIME

# Preserve DB-clock evidence precision on TiDB/MySQL; SQLite stays portable.
DateTime = SQLDateTime().with_variant(DATETIME(fsp=6), "mysql")
metadata = MetaData()
ID_TYPE = BigInteger().with_variant(Integer, "sqlite")
STAT_FIELDS = (
    "used",
    "capacity",
    "total_used",
    "expected_capacity",
    "expected_seconds",
    "observed_seconds",
    "usage_seconds",
    "unknown_seconds",
    "zero_seconds",
)


def metric_columns(*, peaks=False):
    columns = []
    for metric in ("cpu", "memory"):
        columns.extend(
            Column(f"{metric}_{field}", Float, nullable=False, default=0) for field in STAT_FIELDS
        )
        if peaks:
            columns.extend((Column(f"{metric}_peak", Float), Column(f"{metric}_peak_at", DateTime)))
    return columns


machines = Table(
    "machine_utilization_daily",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    Column("account_id", String(32), nullable=False),
    Column("region", String(64), nullable=False),
    Column("cluster_id", String(128), nullable=False, default=""),
    Column("machine_id", String(255), nullable=False),
    Column("usage_date", Date, nullable=False),
    Column("resource_class", String(32), nullable=False),
    Column("instance_type", String(128), nullable=False, default=""),
    Column("cpu_cores", Float),
    Column("memory_bytes", Float),
    *metric_columns(peaks=True),
    Column("quality_json", JSON, nullable=False, default=list),
    Column("cutoff", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint(
        "account_id",
        "region",
        "cluster_id",
        "machine_id",
        "usage_date",
        name="uq_machine_utilization_day",
    ),
)
Index("ix_machine_utilization_date", machines.c.account_id, machines.c.usage_date)

workloads = Table(
    "workload_utilization_daily",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    Column("account_id", String(32), nullable=False),
    Column("usage_date", Date, nullable=False),
    Column("group_key", String(64), nullable=False),
    Column("labels_json", JSON, nullable=False),
    Column("cpu_request_cores", Float),
    Column("memory_request_bytes", Float),
    *metric_columns(),
    Column("quality_json", JSON, nullable=False, default=list),
    Column("cutoff", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    UniqueConstraint("account_id", "usage_date", "group_key", name="uq_workload_utilization_day"),
)
Index("ix_workload_expiry", workloads.c.expires_at, workloads.c.id)

contexts = Table(
    "pod_utilization_context",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    Column("context_key", String(64), nullable=False, unique=True),
    Column("account_id", String(32), nullable=False),
    Column("cluster_id", String(128), nullable=False),
    Column("namespace", String(128), nullable=False),
    Column("pod_uid", String(255), nullable=False),
    Column("pod_name", String(255), nullable=False),
    Column("node_query_kind", String(32), nullable=False, default=""),
    Column("node_query_value", String(255), nullable=False, default=""),
    Column("cpu_request_cores", Float),
    Column("memory_request_bytes", Float),
    Column("labels_json", JSON, nullable=False),
    Column("observed_from", DateTime, nullable=False),
    Column("observed_until", DateTime, nullable=False),
    Column("closed_at", DateTime),
    Column("planned_until", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
)
Index("ix_pod_context_expiry", contexts.c.expires_at, contexts.c.id)
Index("ix_pod_context_identity", contexts.c.account_id, contexts.c.cluster_id, contexts.c.pod_uid)

tasks = Table(
    "utilization_task",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("account_id", String(32), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("context_id", BigInteger),
    Column("payload_json", JSON, nullable=False),
    Column("window_start", DateTime, nullable=False),
    Column("window_end", DateTime, nullable=False),
    Column("due_at", DateTime, nullable=False),
    Column("deadline_at", DateTime, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("lease_token", String(64)),
    Column("lease_until", DateTime),
    Column("dispatched_at", DateTime),
    Column("updated_at", DateTime, nullable=False),
)
Index("ix_utilization_task_due", tasks.c.account_id, tasks.c.status, tasks.c.due_at)
Index("ix_utilization_task_expiry", tasks.c.deadline_at, tasks.c.id)
TABLES = (machines, workloads, contexts, tasks)
