from __future__ import annotations

import logging
import random
import socket
import time
from collections import deque
from contextlib import ExitStack

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


def _connect_api_tcp(dialect, args, params, addresses, deadline):
    """Share one TCP budget across DNS answers; PyMySQL still owns TLS and authentication."""
    conn = dialect.connect(*args, **params, defer_connect=True)
    if conn.unix_socket:  # Can also come from a MySQL option file.
        conn.connect()
        return conn

    try:
        if not addresses:
            addresses.extend(dict.fromkeys(socket.getaddrinfo(conn.host, conn.port, type=socket.SOCK_STREAM)))
        if not addresses:
            raise OSError("no TCP addresses returned")
        for left in range(len(addresses), 0, -1):
            family, kind, proto, _, peer = addresses[0]
            addresses.rotate(-1)  # A failed greeting should not retry the same peer first.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TCP connection budget exhausted")
            try:
                with ExitStack() as cleanup:
                    sock = socket.socket(family, kind, proto)
                    cleanup.callback(sock.close)
                    sock.settimeout(remaining / left)
                    if conn.bind_address is not None:
                        sock.bind((conn.bind_address, 0))
                    sock.connect(peer)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    cleanup.pop_all()  # Transfer ownership to the driver below.
                break
            except OSError:
                if left == 1:
                    raise
    except OSError as exc:
        raise dialect.loaded_dbapi.OperationalError(2003, f"Cannot connect to MySQL ({exc})") from exc

    # Do not put a numeric address in conn.host: TLS SNI / identity verification
    # must use the configured hostname. Once passed in, PyMySQL owns the socket.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        sock.close()
        raise dialect.loaded_dbapi.OperationalError(2003, "MySQL connection timed out before greeting")
    conn._read_timeout = min(params["read_timeout"], remaining)
    conn._write_timeout = min(params["write_timeout"], remaining)
    conn.host_info = f"socket {conn.host}:{conn.port}"
    conn.connect(sock=sock)
    return conn


def _install_connection_retry(engine: Engine, *, retry_connect_timeout: bool = False) -> None:
    """Retry transport failures only before handing a new PyMySQL connection to SQLAlchemy.

    Only for engines built here with explicit connect/read/write timeout arguments.
    The API can reserve part of its connect budget for a second handshake on
    timeout. No checked-out connection, business SQL or transaction is replayed.
    """
    if engine.dialect.name != "mysql" or engine.dialect.driver != "pymysql":
        return

    @event.listens_for(engine, "do_connect")
    def _connect(dialect, _record, args, params):
        if not all(name in params for name in ("connect_timeout", "read_timeout", "write_timeout")):
            raise ValueError("Connection retry requires explicit connect/read/write timeouts")
        deadline = time.monotonic() + params["connect_timeout"]
        addresses = deque()  # Per fresh connection, never a process-wide DNS cache.
        for attempt in range(2):
            now = time.monotonic()
            remaining = max(0.001, deadline - now)
            if retry_connect_timeout and attempt == 0 and remaining >= (
                2 * (MIN_CONNECT_RETRY_BUDGET_SECONDS + CONNECT_RETRY_JITTER_SECONDS[1])
            ):
                remaining /= 2  # reserve room for a fresh connection, not a query retry
            connect_params = dict(params)
            # Also bound greeting/TLS/init-command reads, not just the TCP connect.
            for name in ("connect_timeout", "read_timeout", "write_timeout"):
                connect_params[name] = min(params[name], remaining)
            try:
                if retry_connect_timeout:
                    connection = _connect_api_tcp(dialect, args, connect_params, addresses, now + remaining)
                else:
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
                timed_out = "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
                if (attempt or code not in {2003, 2006, 2013}
                        or (timed_out and not retry_connect_timeout)
                        or time.monotonic() + delay + MIN_CONNECT_RETRY_BUDGET_SECONDS > deadline):
                    raise
                logger.warning("Retrying database connection after transport failure (errno=%s)", code)
                time.sleep(delay)
                if deadline - time.monotonic() < MIN_CONNECT_RETRY_BUDGET_SECONDS:
                    # Sleep/scheduling may have consumed more budget than requested.
                    raise


def _install_pool_socket_budgets(engine: Engine, database: DatabaseSettings) -> None:
    if engine.dialect.name != "mysql" or engine.dialect.driver != "pymysql":
        return

    # SQLAlchemy initializes new connections (SET NAMES) and pre-pings idle ones
    # before its checkout event. A silent socket must not get a business-query
    # timeout in either path. Leave disconnect classification/replacement to the pool.
    @event.listens_for(engine, "connect", insert=True)
    @event.listens_for(engine, "checkin")
    def _use_probe_budget(connection, _record):
        if connection is not None:  # invalidated connections can check in as None
            connection._read_timeout = min(database.connect_timeout_seconds, database.read_timeout_seconds)
            connection._write_timeout = min(database.connect_timeout_seconds, database.write_timeout_seconds)

    @event.listens_for(engine, "checkout")
    def _use_query_budget(connection, _record, _proxy):
        connection._read_timeout = database.read_timeout_seconds
        connection._write_timeout = database.write_timeout_seconds


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


def build_engine(settings: Settings | None = None, *, retry_connect_timeout: bool = False) -> Engine:
    resolved = settings or get_settings()
    url = resolved.database.url or URL.create(
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
    _install_connection_retry(engine, retry_connect_timeout=retry_connect_timeout)
    _install_pool_socket_budgets(engine, resolved.database)
    return engine
