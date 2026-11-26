"""V3 owned tables. Creation is an explicit offline migration/operator action."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Date,
    Double,
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
from sqlalchemy.dialects.mysql import DATETIME

# Preserve DB-clock evidence precision on TiDB/MySQL; SQLite stays portable.
DateTime = SQLDateTime().with_variant(DATETIME(fsp=6), "mysql")
metadata = MetaData()
ID_TYPE = BigInteger().with_variant(Integer, "sqlite")
_MYSQL_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_bin"}

_machine_stats = (
    Column("cpu_percent_sum", Double, nullable=False),
    Column("cpu_sample_minutes", Integer, nullable=False),
    Column("cpu_peak_percent", Double),
    Column("memory_percent_sum", Double, nullable=False),
    Column("memory_sample_minutes", Integer, nullable=False),
    Column("memory_peak_percent", Double),
)
_workload_stats = (
    Column("cpu_used", Double, nullable=False),
    Column("cpu_requested", Double, nullable=False),
    Column("memory_used", Double, nullable=False),
    Column("memory_requested", Double, nullable=False),
    Column("cpu_matched_pod_minutes", BigInteger, nullable=False),
    Column("memory_matched_pod_minutes", BigInteger, nullable=False),
    Column("expected_pod_minutes", BigInteger, nullable=False),
)
MACHINE_STATS = tuple(column.name for column in _machine_stats)
WORKLOAD_STATS = tuple(column.name for column in _workload_stats)


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
    *_machine_stats,
    UniqueConstraint(
        "account_id",
        "region",
        "cluster_id",
        "machine_id",
        "usage_date",
        name="uq_machine_utilization_day",
    ),
    **_MYSQL_OPTIONS,
)
Index("ix_machine_utilization_date", machines.c.account_id, machines.c.usage_date)

workloads = Table(
    "workload_utilization_daily",
    metadata,
    Column("account_id", String(32), primary_key=True),
    Column("usage_date", Date, primary_key=True),
    Column("group_key", String(64), primary_key=True),
    Column("labels_json", JSON, nullable=False),
    *_workload_stats,
    **_MYSQL_OPTIONS,
)

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
    Column("node_instance_id", String(255)),
    Column("cpu_request_cores", Double),
    Column("memory_request_bytes", Double),
    Column("labels_json", JSON, nullable=False),
    Column("observed_from", DateTime, nullable=False),
    Column("observed_until", DateTime, nullable=False),
    Column("closed_at", DateTime),
    Column("planned_until", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    **_MYSQL_OPTIONS,
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
    Column("lease_token", String(64)),
    Column("lease_until", DateTime),
    Column("dispatched_at", DateTime),
    **_MYSQL_OPTIONS,
)
Index("ix_utilization_task_due", tasks.c.account_id, tasks.c.status, tasks.c.due_at)
Index("ix_utilization_task_expiry", tasks.c.deadline_at, tasks.c.id)
Index("ix_utilization_task_context", tasks.c.context_id)
TABLES = (machines, workloads, contexts, tasks)
