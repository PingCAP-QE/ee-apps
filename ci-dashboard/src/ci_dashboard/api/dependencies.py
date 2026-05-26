from __future__ import annotations

from functools import lru_cache

from sqlalchemy.engine import Engine

from ci_dashboard.common.db import build_engine


@lru_cache(maxsize=1)
def _cached_engine() -> Engine:
    return build_engine()


def get_engine() -> Engine:
    return _cached_engine()
