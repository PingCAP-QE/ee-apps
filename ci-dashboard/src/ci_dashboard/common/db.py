from __future__ import annotations

import logging
import random
import time

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL

from .config import DatabaseSettings, Settings, get_settings
from ci_dashboard.jobs.build_url_matcher import (
    normalize_build_url,
    normalize_github_actions_build_url,
    normalized_job_path_from_key,
)

logger = logging.getLogger(__name__)

POOL_RECYCLE_SECONDS = 900
CONNECT_RETRY_JITTER_SECONDS = (0.05, 0.15)
MIN_CONNECT_RETRY_BUDGET_SECONDS = 1.0


def _build_connect_args(database: DatabaseSettings) -> dict[str, object]:
    if database.url and database.url.startswith("sqlite"):
        return {}
    connect_args: dict[str, object] = {
        "connect_timeout": database.connect_timeout_seconds,
        "read_timeout": database.read_timeout_seconds,
        "write_timeout": database.write_timeout_seconds,
        "init_command": (
            f"SET SESSION MAX_EXECUTION_TIME={database.query_timeout_seconds * 1000}"
        ),
    }
    if database.ssl_ca:
        connect_args["ssl"] = {"ca": database.ssl_ca}
    return connect_args


def _build_engine_kwargs(database: DatabaseSettings) -> dict[str, object]:
    engine_kwargs = {
        "pool_pre_ping": True,
        "future": True,
        "connect_args": _build_connect_args(database),
    }
    if database.url and database.url.startswith("sqlite"):
        return engine_kwargs
    return {
        **engine_kwargs,
        "pool_size": database.pool_size,
        "max_overflow": database.max_overflow,
        "pool_timeout": database.pool_timeout_seconds,
        # Proactively retire API pool connections before the 20-minute NAT idle limit.
        "pool_recycle": POOL_RECYCLE_SECONDS,
    }


def _install_connection_retry(engine: Engine) -> None:
    """Retry transport failures only before handing a new PyMySQL connection to SQLAlchemy.

    Only for engines built here with explicit connect/read/write timeout arguments.
    No SQL statement or transaction is replayed here. Query/socket timeouts are
    deliberately not retried: repeating expensive work can worsen an outage.
    """
    if engine.dialect.name != "mysql" or engine.dialect.driver != "pymysql":
        return

    @event.listens_for(engine, "do_connect")
    def _connect(dialect, _record, args, params):
        if not all(name in params for name in ("connect_timeout", "read_timeout", "write_timeout")):
            raise ValueError("Connection retry requires explicit connect/read/write timeouts")
        deadline = time.monotonic() + params["connect_timeout"]
        for attempt in range(2):
            remaining = max(0.001, deadline - time.monotonic())
            connect_params = dict(params)
            # Also bound greeting/TLS/init-command reads, not just the TCP connect.
            for name in ("connect_timeout", "read_timeout", "write_timeout"):
                connect_params[name] = min(params[name], remaining)
            try:
                connection = dialect.connect(*args, **connect_params)
                if not all(hasattr(connection, name) for name in ("_read_timeout", "_write_timeout")):
                    connection.close()
                    raise RuntimeError("Unsupported PyMySQL connection timeout interface")
                # PyMySQL consults these fields before every subsequent socket I/O.
                # Restore business-query timeouts after the handshake, not on its socket.
                connection._read_timeout = params["read_timeout"]
                connection._write_timeout = params["write_timeout"]
                return connection
            except dialect.loaded_dbapi.OperationalError as exc:
                code = exc.args[0] if exc.args else None
                delay = random.uniform(*CONNECT_RETRY_JITTER_SECONDS)
                # Reserve at least a second after jitter for a useful handshake retry.
                if (attempt or code not in {2003, 2006, 2013}
                        or "timed out" in str(exc).lower()
                        or "timeout" in str(exc).lower()
                        or time.monotonic() + delay + MIN_CONNECT_RETRY_BUDGET_SECONDS > deadline):
                    raise
                logger.warning("Retrying database connection after transport failure (errno=%s)", code)
                time.sleep(delay)
                if deadline - time.monotonic() < MIN_CONNECT_RETRY_BUDGET_SECONDS:
                    # Sleep/scheduling may have consumed more budget than requested.
                    raise


def install_sqlite_functions(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _register_functions(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function(
            "normalized_job_path_from_key",
            1,
            normalized_job_path_from_key,
        )
        dbapi_connection.create_function(
            "normalize_build_url",
            1,
            normalize_build_url,
        )
        dbapi_connection.create_function(
            "normalize_github_actions_build_url",
            1,
            normalize_github_actions_build_url,
        )


def build_engine(settings: Settings | None = None) -> Engine:
    resolved = settings or get_settings()
    if resolved.database.url:
        engine = create_engine(
            resolved.database.url,
            **_build_engine_kwargs(resolved.database),
        )
        install_sqlite_functions(engine)
        _install_connection_retry(engine)
        return engine
    url = URL.create(
        drivername="mysql+pymysql",
        username=resolved.database.user,
        password=resolved.database.password,
        host=resolved.database.host,
        port=resolved.database.port,
        database=resolved.database.database,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(
        url,
        **_build_engine_kwargs(resolved.database),
    )
    install_sqlite_functions(engine)
    _install_connection_retry(engine)
    return engine
