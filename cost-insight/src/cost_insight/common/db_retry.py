"""Opt-in replay for transactions whose fixed scope and inputs are idempotent."""
from __future__ import annotations

import errno
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from pymysql.err import OperationalError
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

LOG = logging.getLogger(__name__)
T = TypeVar("T")


def is_transport_disconnect(error: BaseException) -> bool:
    """Do not confuse client/query timeouts or arbitrary invalidations with EOF."""
    origin = error.orig if isinstance(error, DBAPIError) else error
    cause = origin
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if (isinstance(cause, TimeoutError) or "timeout" in str(cause).lower()
                or "timed out" in str(cause).lower()):
            return False
        cause = cause.__cause__ or cause.__context__
    if isinstance(origin, OperationalError):
        return bool(origin.args) and origin.args[0] in {2006, 2013}
    return (
        isinstance(error, DBAPIError)
        and error.connection_invalidated
        and isinstance(origin, OSError)
        and origin.errno in {errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ENETRESET}
    )


def replay_safe_transaction(engine: Engine, operation: Callable[[Connection], T]) -> T:
    """At most 3 whole-transaction attempts, including an unknown commit outcome.

    Callers MUST use the same fixed scope/input on every attempt, including after
    commit ACK loss. Iterators/counters must be recreated inside operation. This
    is deliberately not an engine event or an individual-statement retry.
    """
    for attempt in range(3):
        try:
            with engine.begin() as connection:
                return operation(connection)
        except Exception as exc:
            if attempt == 2 or not is_transport_disconnect(exc):
                raise
            # Only static metadata: never log the exception, SQL or parameters.
            LOG.warning("Replaying safe database transaction (attempt=%s/3)", attempt + 2)
            time.sleep(random.uniform(0.5, 1.0) * min(2 ** attempt, 2))
