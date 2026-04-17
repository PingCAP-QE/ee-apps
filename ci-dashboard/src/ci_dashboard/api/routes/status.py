from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Engine

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.queries.status import get_freshness


router = APIRouter(prefix="/api/v1/status", tags=["status"])


@router.get("/freshness")
def freshness(engine: Engine = Depends(get_engine)) -> dict[str, object]:
    return get_freshness(engine)
