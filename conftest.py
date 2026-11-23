"""Shared pytest fixtures."""
import os
import time

import pytest
from sqlalchemy import create_engine

import arxiv_scraper
from models import Base

DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="set TEST_DATABASE_URL to run MySQL tests")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Rate limiting and backoff would make tests slow; skip the waits."""
    monkeypatch.setattr(time, "sleep", lambda _: None)


@pytest.fixture
def engine():
    """A fresh schema in the throwaway TEST_DATABASE_URL database."""
    eng = create_engine(DB_URL)
    Base.metadata.drop_all(eng)
    eng.dispose()
    eng = arxiv_scraper.make_engine(DB_URL)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()
