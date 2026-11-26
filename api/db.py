"""Database access for the API: one engine for the app, one session per request."""
from __future__ import annotations

from functools import lru_cache
from typing import Iterator

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from arxiv_scraper import database_url_from_env, make_engine  # also loads .env


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Created on first use; make_engine also creates any missing tables and indexes."""
    url = database_url_from_env()
    if not url:
        raise RuntimeError("No database configured: set DB_NAME etc. in .env (see .env.example)")
    return make_engine(url)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: a session that is closed when the request finishes."""
    with Session(get_engine()) as session:
        yield session
