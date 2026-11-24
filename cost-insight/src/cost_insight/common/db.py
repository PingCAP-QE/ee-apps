from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url

from cost_insight.common.config import DatabaseSettings, Settings, get_settings


def build_engine(settings: Settings | None = None) -> Engine:
    resolved = settings or get_settings()
    if resolved.database.url:
        return create_engine(
            resolved.database.url,
            pool_pre_ping=True,
            future=True,
            connect_args=_build_connect_args(resolved.database),
        )

    url = URL.create(
        drivername="mysql+pymysql",
        username=resolved.database.user,
        password=resolved.database.password,
        host=resolved.database.host,
        port=resolved.database.port,
        database=resolved.database.database,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        connect_args=_build_connect_args(resolved.database),
    )


def _build_connect_args(database: DatabaseSettings) -> dict[str, object]:
    if database.url and make_url(database.url).get_backend_name() != "mysql":
        return {}
    args: dict[str, object] = {
        "connect_timeout": database.connect_timeout_seconds,
        "read_timeout": database.read_timeout_seconds,
        "write_timeout": database.write_timeout_seconds,
    }
    if database.ssl_ca:
        args["ssl"] = {"ca": database.ssl_ca}
    return args
