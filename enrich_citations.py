#!/usr/bin/env python3
"""
enrich_citations.py - Add Semantic Scholar citation counts to the papers table.

For papers that have never been looked up, or whose counts are older than
--refresh-days, looks up 500 arXiv IDs per request with the Semantic Scholar
batch endpoint and writes:
    citation_count              - all citations Semantic Scholar has found
    influential_citation_count  - citations where the paper shaped the citing work
    citations_updated_at        - when the lookup ran

Run it after arxiv_scraper.py, and again periodically, because counts grow.

Usage
    # database settings and the optional S2_API_KEY go in .env (see .env.example)
    python enrich_citations.py                       # all due papers
    python enrich_citations.py --categories cs.CL    # only papers listed in cs.CL
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy import Engine, Select, func, or_, select, update
from sqlalchemy.orm import Session

from arxiv_scraper import database_url_from_env, make_engine
from models import Paper, utcnow
from taxonomy import expand

S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_FIELDS = "citationCount,influentialCitationCount"
BATCH_SIZE = 500                 # Semantic Scholar's maximum IDs per batch request
INTERVAL_WITH_KEY_S = 1.1        # introductory key limit: 1 request/second
INTERVAL_WITHOUT_KEY_S = 3.0     # unauthenticated users share one pool; be gentle
MAX_RETRIES = 6

log = logging.getLogger("enrich_citations")


class SemanticScholarError(RuntimeError):
    pass


class SemanticScholarClient:
    def __init__(self, api_key: Optional[str] = None, min_interval: Optional[float] = None,
                 session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "arxiv-academic-scraper/1.0 (citation enrichment)"
        if api_key:
            self.session.headers["x-api-key"] = api_key
        self.min_interval = min_interval if min_interval is not None else (
            INTERVAL_WITH_KEY_S if api_key else INTERVAL_WITHOUT_KEY_S)
        self._last_request = 0.0
        self.requests_made = 0

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(300.0, 5.0 * 2 ** (attempt - 1)) + random.uniform(0, 2)

    def citations(self, arxiv_ids: list[str]) -> list[Optional[dict]]:
        """
        Look up up to 500 arXiv IDs. Returns one item per ID, in the same order:
        {"citationCount": int, "influentialCitationCount": int}, or None when
        Semantic Scholar doesn't know the paper.
        """
        if len(arxiv_ids) > BATCH_SIZE:
            raise ValueError(f"At most {BATCH_SIZE} IDs per request")
        payload = {"ids": [f"ARXIV:{i}" for i in arxiv_ids]}
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            self.requests_made += 1
            try:
                resp = self.session.post(S2_BATCH_URL, params={"fields": S2_FIELDS},
                                         json=payload, timeout=120)
            except requests.RequestException as exc:
                wait = self._backoff(attempt)
                log.warning("Network error (%s); retry %d/%d in %.0fs", exc, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 200:
                results = resp.json()
                if not isinstance(results, list) or len(results) != len(arxiv_ids):
                    raise SemanticScholarError(f"Unexpected response shape: {str(results)[:200]}")
                return results
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else self._backoff(attempt)
                log.warning("HTTP %s; retry %d/%d in %.0fs", resp.status_code, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            raise SemanticScholarError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        raise SemanticScholarError(f"Giving up after {MAX_RETRIES} attempts")


def due_papers(cutoff: datetime, categories: Optional[list[str]] = None) -> Select:
    """Papers never looked up, or looked up before `cutoff`; never-looked-up first."""
    stmt = select(Paper.arxiv_id).where(
        or_(Paper.citations_updated_at.is_(None), Paper.citations_updated_at < cutoff))
    if categories:
        # JSON_CONTAINS(other_categories, '"cs.RO"'): is cs.RO in the JSON array?
        stmt = stmt.where(or_(Paper.primary_category.in_(categories),
                              *[func.json_contains(Paper.other_categories, func.json_quote(c))
                                for c in categories]))
    # MySQL sorts NULLs first in ascending order, so never-checked papers come first.
    return stmt.order_by(Paper.citations_updated_at.asc(), Paper.announced_date.desc(),
                         Paper.arxiv_id)


def enrich(engine: Engine, client: SemanticScholarClient, refresh_days: int = 30,
           categories: Optional[list[str]] = None, limit: Optional[int] = None) -> dict[str, int]:
    """Look up citation counts for every due paper. Returns counters for reporting."""
    cutoff = utcnow() - timedelta(days=refresh_days)
    query = due_papers(cutoff, categories)
    with Session(engine) as s:
        due = s.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    target = min(due, limit) if limit else due
    log.info("%d papers due for a citation lookup; processing %d (%d requests)",
             due, target, -(-target // BATCH_SIZE))

    stats = {"processed": 0, "found": 0, "not_found": 0}
    seen: set[str] = set()
    while stats["processed"] < target:
        size = min(BATCH_SIZE, target - stats["processed"])
        with Session(engine) as s:
            # Rows updated below get a timestamp after `cutoff`, so they drop out of
            # this query; re-querying each time therefore walks through all due papers.
            ids = list(s.scalars(query.limit(size)))
        if not ids or seen.intersection(ids):  # safety net: never loop over the same papers
            break
        seen.update(ids)
        results = client.citations(ids)
        now = utcnow()
        rows = []
        for arxiv_id, hit in zip(ids, results):
            rows.append({
                "arxiv_id": arxiv_id,
                "citation_count": hit.get("citationCount") if hit else None,
                "influential_citation_count": hit.get("influentialCitationCount") if hit else None,
                "citations_updated_at": now,
            })
            stats["found" if hit else "not_found"] += 1
        with Session(engine) as s, s.begin():
            s.execute(update(Paper), rows)  # bulk UPDATE by primary key
        stats["processed"] += len(ids)
        log.info("[%d/%d] looked up %d papers (%d found, %d unknown to Semantic Scholar so far)",
                 stats["processed"], target, len(ids), stats["found"], stats["not_found"])
    return stats


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Add Semantic Scholar citation counts to the papers table.")
    p.add_argument("--db-url", default=database_url_from_env(),
                   help="SQLAlchemy URL (default: DATABASE_URL or DB_* settings from .env)")
    p.add_argument("--api-key", default=os.environ.get("S2_API_KEY"),
                   help="Semantic Scholar API key, optional (default: $S2_API_KEY)")
    p.add_argument("--categories", nargs="+",
                   help="only papers listed in these categories/archives (primary or cross-list)")
    p.add_argument("--refresh-days", type=int, default=30,
                   help="re-check counts older than this many days (default: 30)")
    p.add_argument("--limit", type=int, help="stop after this many papers")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.db_url:
        p.error("no database configured: set DB_NAME etc. in .env, or pass --db-url")
    if args.refresh_days < 0:
        p.error("--refresh-days must be 0 or more")
    if args.categories:
        try:
            args.categories = expand(args.categories)
        except ValueError as exc:
            p.error(str(exc))
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not args.api_key:
        log.info("No Semantic Scholar API key: using the shared public pool (slower, may hit 429s).")
    engine = make_engine(args.db_url)
    client = SemanticScholarClient(api_key=args.api_key)
    try:
        stats = enrich(engine, client, args.refresh_days, args.categories, args.limit)
    except KeyboardInterrupt:
        log.info("Interrupted - finished batches are saved; rerun to continue.")
        return 130
    finally:
        engine.dispose()
    log.info("Done: %d papers looked up, %d found, %d unknown; %d API requests.",
             stats["processed"], stats["found"], stats["not_found"], client.requests_made)
    return 0


if __name__ == "__main__":
    sys.exit(main())
