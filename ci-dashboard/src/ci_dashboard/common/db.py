from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL

from .config import DatabaseSettings, Settings, get_settings
from ci_dashboard.jobs.build_url_matcher import normalize_build_url, normalized_job_path_from_key


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
    }


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


def build_engine(settings: Settings | None = None) -> Engine:
    resolved = settings or get_settings()
    if resolved.database.url:
        engine = create_engine(
            resolved.database.url,
            **_build_engine_kwargs(resolved.database),
        )
        install_sqlite_functions(engine)
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
    return engine
