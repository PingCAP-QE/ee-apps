from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL

from .config import DatabaseSettings, Settings, get_settings
from ci_dashboard.jobs.build_url_matcher import normalize_build_url, normalized_job_path_from_key


def _build_connect_args(database: DatabaseSettings) -> dict[str, object]:
    if not database.ssl_ca:
        return {}
    return {
        "ssl": {
            "ca": database.ssl_ca,
        }
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
            pool_pre_ping=True,
            future=True,
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
        pool_pre_ping=True,
        future=True,
        connect_args=_build_connect_args(resolved.database),
    )
    install_sqlite_functions(engine)
    return engine
