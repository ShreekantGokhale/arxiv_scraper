#!/usr/bin/env python3
"""
arxiv_scraper.py - Build a MySQL database of arXiv paper metadata.

Harvests the official arXiv API (https://export.arxiv.org/api/query) one
(category, submission day) pair at a time, so every query is small, each
pair's result count is recorded, and an interrupted run resumes exactly
where it stopped.

Stored attributes per paper
    title, authors, primary (main) category, other categories,
    announcement date, abstract, full-paper (PDF) link
plus arxiv_id, latest_version, submitted_at, abs_url, scraped_at.

Etiquette (arXiv API Terms of Use)
    * at most one request every 3 seconds, one connection at a time
    * a descriptive User-Agent with a contact email
    * honour 429/503 + Retry-After, back off on errors

Usage
    # connection settings in .env (see .env.example), or:
    export DATABASE_URL='mysql+pymysql://user:pass@localhost:3306/arxiv?charset=utf8mb4'
    python arxiv_scraper.py --list-categories
    python arxiv_scraper.py --email you@university.edu \
        --start 2022-11-21 --end 2026-11-20 --categories cs.AI cs.CL
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import URL, Engine, create_engine, func, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from models import EXTRA_INDEXES, LEGACY_INDEXES, Base, Category, CrawlLog, Paper, utcnow
from taxonomy import CATEGORIES, TAXONOMY, expand

try:  # read DATABASE_URL / S2_API_KEY from a .env file next to the scripts, if present
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_URL = "https://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
EASTERN = ZoneInfo("America/New_York")  # arXiv's schedule is in US Eastern time

MIN_INTERVAL_S = 3.0      # Terms of Use: <= 1 request every 3 seconds
PAGE_SIZE = 1000          # API allows up to 2000; 1000 keeps responses fast
MAX_RETRIES = 6
MAX_EMPTY_PAGE_RETRIES = 3
INSERT_CHUNK = 1000       # rows per INSERT statement

DEFAULT_CATEGORIES = ["cs.AI", "cs.CL", "cs.CV", "cs.LG"]

log = logging.getLogger("arxiv_scraper")


# --------------------------------------------------------------------------
# Parsed record (mirrors models.Paper, minus server-filled columns)
# --------------------------------------------------------------------------
@dataclass
class PaperRecord:
    arxiv_id: str
    latest_version: int
    title: str
    authors: list[str]
    primary_category: str
    other_categories: list[str]
    submitted_at: datetime        # naive UTC (MySQL DATETIME has no time zone)
    announced_date: date
    abstract: str
    abs_url: str
    pdf_url: str


# --------------------------------------------------------------------------
# Announcement date
# --------------------------------------------------------------------------
def announcement_date(submitted_utc: datetime) -> date:
    """
    Map a submission timestamp to the day arXiv announces it.

    arXiv's published schedule (US Eastern time):
        Mon 14:00 - Tue 14:00  -> announced Tue 20:00
        Tue 14:00 - Wed 14:00  -> announced Wed 20:00
        Wed 14:00 - Thu 14:00  -> announced Thu 20:00
        Thu 14:00 - Fri 14:00  -> announced Sun 20:00
        Fri 14:00 - Mon 14:00  -> announced Mon 20:00

    Holiday deferrals and papers placed "on hold" by moderators are not
    modelled, so treat the result as the scheduled announcement date.
    """
    local = submitted_utc.astimezone(EASTERN)
    day = local.date()
    if local.weekday() < 5 and local.time() < dtime(14, 0):
        deadline = day                      # makes today's 14:00 cutoff
    else:
        deadline = day + timedelta(days=1)  # rolls to the next weekday cutoff
        while deadline.weekday() >= 5:
            deadline += timedelta(days=1)
    # The Friday cutoff is announced on Sunday evening.
    return deadline + timedelta(days=2) if deadline.weekday() == 4 else deadline


# --------------------------------------------------------------------------
# Parsing the Atom feed
# --------------------------------------------------------------------------
_ARXIV_CATEGORY_RE = re.compile(r"^[a-z][a-z\-]*(\.[A-Za-z][A-Za-z\-]*)?$")  # cs.AI, cond-mat.soft
_ID_RE = re.compile(r"/abs/(.+?)(?:v(\d+))?$")


def _clean(text: Optional[str]) -> Optional[str]:
    return " ".join(text.split()) if text else None


def _text(elem: ET.Element, path: str) -> Optional[str]:
    node = elem.find(path, NS)
    return _clean(node.text) if node is not None and node.text else None


class ArxivAPIError(RuntimeError):
    pass


def parse_feed(xml_bytes: bytes) -> tuple[int, list[PaperRecord]]:
    """Parse one API response page into (totalResults, records)."""
    root = ET.fromstring(xml_bytes)
    total_node = root.find("opensearch:totalResults", NS)
    total = int(total_node.text) if total_node is not None and total_node.text else 0

    records: list[PaperRecord] = []
    for entry in root.findall("atom:entry", NS):
        raw_id = _text(entry, "atom:id") or ""
        if "/api/errors" in raw_id:  # the API reports bad queries as an entry
            raise ArxivAPIError(_text(entry, "atom:summary") or "arXiv API error")
        records.append(parse_entry(entry))
    return total, records


def parse_entry(entry: ET.Element) -> PaperRecord:
    raw_id = _text(entry, "atom:id") or ""
    m = _ID_RE.search(raw_id)
    if not m:
        raise ValueError(f"Unrecognised arXiv id: {raw_id!r}")
    arxiv_id, version = m.group(1), int(m.group(2) or 1)

    primary_node = entry.find("arxiv:primary_category", NS)
    primary = primary_node.get("term") if primary_node is not None else None
    categories = [c.get("term") for c in entry.findall("atom:category", NS) if c.get("term")]
    # Drop ACM / MSC class codes (e.g. "I.2.7", "68T05"); keep arXiv categories only.
    categories = [c for c in dict.fromkeys(categories) if _ARXIV_CATEGORY_RE.match(c)]
    primary = primary or (categories[0] if categories else "unknown")

    published = _text(entry, "atom:published") or ""
    submitted = datetime.fromisoformat(published.replace("Z", "+00:00"))

    pdf_url = None
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf":
            pdf_url = link.get("href")
    pdf_url = (pdf_url or f"https://arxiv.org/pdf/{arxiv_id}").replace("http://", "https://", 1)

    return PaperRecord(
        arxiv_id=arxiv_id,
        latest_version=version,
        title=_text(entry, "atom:title") or "",
        authors=[_clean(a.findtext("atom:name", default="", namespaces=NS)) or ""
                 for a in entry.findall("atom:author", NS)],
        primary_category=primary,
        other_categories=[c for c in categories if c != primary],
        submitted_at=submitted.astimezone(timezone.utc).replace(tzinfo=None),  # naive UTC
        announced_date=announcement_date(submitted),
        abstract=_text(entry, "atom:summary") or "",
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=pdf_url,
    )


# --------------------------------------------------------------------------
# HTTP client: rate limiting + retries
# --------------------------------------------------------------------------
class ArxivClient:
    def __init__(self, email: str, min_interval: float = MIN_INTERVAL_S,
                 session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = f"arxiv-academic-scraper/1.0 (mailto:{email})"
        self.min_interval = min_interval
        self._last_request = 0.0
        self.requests_made = 0

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(300.0, 10.0 * 2 ** (attempt - 1)) + random.uniform(0, 3)

    def get(self, params: dict) -> bytes:
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            self.requests_made += 1
            try:
                resp = self.session.get(API_URL, params=params, timeout=90)
            except requests.RequestException as exc:
                wait = self._backoff(attempt)
                log.warning("Network error (%s); retry %d/%d in %.0fs", exc, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue

            body = resp.content or b""
            # arXiv sometimes answers 200 with a plain-text "Rate exceeded" body.
            rate_limited = resp.status_code == 429 or (
                resp.status_code == 200 and b"<feed" not in body[:2000]
                and b"rate exceeded" in body[:500].lower()
            )
            if resp.status_code == 200 and not rate_limited:
                return body
            if rate_limited or resp.status_code in (500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else self._backoff(attempt)
                log.warning("HTTP %s%s; retry %d/%d in %.0fs", resp.status_code,
                            " (rate limited)" if rate_limited else "", attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        raise ArxivAPIError(f"Giving up after {MAX_RETRIES} attempts: {params}")


# --------------------------------------------------------------------------
# Harvesting one (category, day)
# --------------------------------------------------------------------------
def fetch_category_day(client: ArxivClient, category: str, day: date) -> tuple[int, list[PaperRecord]]:
    """
    Fetch every paper first submitted on `day` (UTC) that is listed in
    `category`, whether as its primary category or as a cross-list.
    """
    stamp = day.strftime("%Y%m%d")
    search = f"cat:{category} AND submittedDate:[{stamp}0000 TO {stamp}2359]"
    records: list[PaperRecord] = []
    start, total, empty_retries = 0, 0, 0

    while True:
        params = {
            "search_query": search,
            "start": start,
            "max_results": PAGE_SIZE,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }
        total, page = parse_feed(client.get(params))
        if not page and start < total:
            # The API occasionally returns an empty page mid-result-set; retry it.
            empty_retries += 1
            if empty_retries > MAX_EMPTY_PAGE_RETRIES:
                log.warning("%s %s: empty page at offset %d of %d; keeping %d papers",
                            category, day, start, total, len(records))
                break
            continue
        empty_retries = 0
        records.extend(page)
        start += len(page)
        if not page or start >= total:
            break
    return total, records


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def database_url_from_env() -> Optional[str | URL]:
    """
    DATABASE_URL if set; otherwise a URL built from DB_USER, DB_PASSWORD, DB_HOST,
    DB_PORT and DB_NAME. Building it with URL.create means passwords containing
    characters like @ : / # need no manual escaping.
    """
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if not os.environ.get("DB_NAME"):
        return None
    return URL.create(
        "mysql+pymysql",
        username=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD") or None,
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        database=os.environ["DB_NAME"],
        query={"charset": "utf8mb4"},
    )


def make_engine(db_url: str | URL) -> Engine:
    engine = create_engine(db_url, pool_pre_ping=True, pool_recycle=3600)
    if engine.dialect.name != "mysql":
        raise ValueError(f"Expected a MySQL URL (mysql+pymysql://...), got {engine.dialect.name!r}")
    Base.metadata.create_all(engine)
    ensure_indexes(engine)
    seed_categories(engine)
    return engine


def ensure_indexes(engine: Engine) -> None:
    """Drop models.LEGACY_INDEXES if present, then create missing models.EXTRA_INDEXES."""
    with engine.connect() as conn:
        existing = set(conn.scalars(text(
            "SELECT DISTINCT index_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'papers'")))
        # Drop first: building another index rebuilds the table, which is what crashes
        # MySQL 8.4 while the multi-valued index exists.
        for name in LEGACY_INDEXES:
            if name in existing:
                log.info("Dropping index %s (no longer used; can crash MySQL 8.4)", name)
                conn.execute(text(f"DROP INDEX {name} ON papers"))
        missing = [name for name in EXTRA_INDEXES if name not in existing]
        if not missing:
            return
        # Adding a FULLTEXT index to a table that already holds rows rebuilds the table, and
        # that has crashed the MySQL server itself (seen on MySQL 9.5 on Windows). On an empty
        # table it's safe, so only ever create these indexes before the first paper is saved.
        if conn.scalar(text("SELECT EXISTS (SELECT 1 FROM papers)")):
            log.warning(
                "Not adding index(es) %s: the papers table already has data, and adding a "
                "FULLTEXT index to a filled table can crash MySQL. Keyword search (q / topic) "
                "is unavailable until you recreate the database; see README, Troubleshooting.",
                ", ".join(missing))
            return
        for name in missing:
            log.info("Creating index %s", name)
            try:
                conn.execute(text(EXTRA_INDEXES[name]))
            except DBAPIError as exc:
                log.warning("Skipping index %s: %s", name, exc.orig)


def _category_row(code: str) -> dict:
    name, archive, group = CATEGORIES.get(code, (None, code.split(".")[0], None))
    return {"code": code, "name": name, "archive": archive, "group_name": group}


def seed_categories(engine: Engine) -> None:
    stmt = mysql_insert(Category).values([_category_row(c) for c in CATEGORIES])
    stmt = stmt.on_duplicate_key_update(
        name=stmt.inserted.name, archive=stmt.inserted.archive, group_name=stmt.inserted.group_name)
    with engine.begin() as conn:
        conn.execute(stmt)


_UPDATE_ON_CONFLICT = ("latest_version", "title", "authors", "primary_category",
                       "other_categories", "abstract", "pdf_url", "scraped_at")


def save_papers(session: Session, records: list[PaperRecord]) -> None:
    """Upsert records: a paper seen again (cross-list or new version) is updated, never duplicated."""
    now = utcnow()
    rows = list({r.arxiv_id: asdict(r) | {"scraped_at": now} for r in records}.values())  # dedupe
    if not rows:
        return
    # A primary category missing from the taxonomy (rare/legacy code) must exist for the FK.
    unknown = {r["primary_category"] for r in rows} - set(CATEGORIES)
    if unknown:
        stmt = mysql_insert(Category).values([_category_row(c) for c in unknown])
        session.execute(stmt.on_duplicate_key_update(code=stmt.inserted.code))  # i.e. do nothing
    for i in range(0, len(rows), INSERT_CHUNK):
        stmt = mysql_insert(Paper).values(rows[i:i + INSERT_CHUNK])
        # Citation columns are deliberately absent: rescraping never overwrites them.
        stmt = stmt.on_duplicate_key_update({col: stmt.inserted[col] for col in _UPDATE_ON_CONFLICT})
        session.execute(stmt)


def mark_done(session: Session, category: str, day: date, total: int, fetched: int) -> None:
    now = utcnow()
    stmt = mysql_insert(CrawlLog).values(category=category, day=day, total_results=total,
                                         fetched=fetched, finished_at=now)
    stmt = stmt.on_duplicate_key_update(total_results=total, fetched=fetched, finished_at=now)
    session.execute(stmt)


# --------------------------------------------------------------------------
# Crawl loop
# --------------------------------------------------------------------------
def daterange(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def plan(engine: Engine, categories: list[str], start: date, end: date,
         order: str = "category") -> list[tuple[str, date]]:
    """All (category, day) pairs in range that are not yet in crawl_log."""
    with Session(engine) as s:
        done = {(c, d) for c, d in s.execute(
            select(CrawlLog.category, CrawlLog.day).where(
                CrawlLog.category.in_(categories), CrawlLog.day.between(start, end)))}
    days = list(daterange(start, end))
    if order == "day":
        pairs = [(c, d) for d in days for c in categories]
    else:
        pairs = [(c, d) for c in categories for d in days]
    return [p for p in pairs if p not in done]


def crawl(engine: Engine, client: ArxivClient, todo: list[tuple[str, date]]) -> None:
    today = datetime.now(timezone.utc).date()
    for i, (category, day) in enumerate(todo, 1):
        total, records = fetch_category_day(client, category, day)
        with Session(engine) as session, session.begin():  # papers + log commit together
            save_papers(session, records)
            if day < today:  # today's submissions are still arriving; don't mark done
                mark_done(session, category, day, total, len(records))
        if i % 25 == 0 or i == len(todo):
            with Session(engine) as s:
                db_total = s.scalar(select(func.count()).select_from(Paper))
            log.info("[%d/%d] %s %s: %d papers | database: %d papers",
                     i, len(todo), category, day, len(records), db_total)
        else:
            log.debug("[%d/%d] %s %s: %d papers", i, len(todo), category, day, len(records))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def print_categories() -> None:
    for group, archives in TAXONOMY.items():
        n = sum(len(c) for c in archives.values())
        print(f"\n{group} ({n})")
        for archive, cats in archives.items():
            for code, name in cats:
                print(f"  {code:<20} {name}")
    print(f"\nTotal: {len(CATEGORIES)} categories. Select by code (cs.AI), archive (cs, astro-ph) or 'all'.")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    p = argparse.ArgumentParser(description="Harvest arXiv metadata into MySQL, per category per day.")
    p.add_argument("--list-categories", action="store_true", help="print all arXiv categories and exit")
    p.add_argument("--email", help="contact email sent in the User-Agent header (required to crawl)")
    p.add_argument("--db-url", default=database_url_from_env(),
                   help="SQLAlchemy URL, e.g. mysql+pymysql://user:pass@localhost:3306/arxiv "
                        "(default: DATABASE_URL, or DB_USER/DB_PASSWORD/DB_HOST/DB_PORT/DB_NAME, "
                        "from the environment or .env)")
    p.add_argument("--start", type=date.fromisoformat,
                   default=yesterday - timedelta(days=4 * 365 + 1),
                   help="first submission day, YYYY-MM-DD (default: 4 years ago)")
    p.add_argument("--end", type=date.fromisoformat, default=yesterday,
                   help="last submission day, YYYY-MM-DD (default: yesterday)")
    p.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES,
                   help="category codes and/or archives, e.g. cs.AI stat.ML or cs, or 'all' "
                        f"(default: {' '.join(DEFAULT_CATEGORIES)})")
    p.add_argument("--order", choices=["category", "day"], default="category",
                   help="'category': finish one category before the next (default); "
                        "'day': all categories for a day, then the next day")
    p.add_argument("--yes", action="store_true", help="skip the confirmation for runs longer than 24h")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.list_categories:
        return args
    if not args.email:
        p.error("--email is required to crawl")
    if not args.db_url:
        p.error("no database configured: set DB_NAME etc. in .env, or pass --db-url")
    if args.start > args.end:
        p.error("--start must not be after --end")
    try:
        args.categories = expand(args.categories)
    except ValueError as exc:
        p.error(str(exc))
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_categories:
        print_categories()
        return 0
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    engine = make_engine(args.db_url)
    todo = plan(engine, args.categories, args.start, args.end, args.order)
    hours = len(todo) * MIN_INTERVAL_S / 3600
    log.info("%d categories x %d days: %d pairs left to fetch (at least %.1f hours at 1 request/%gs)",
             len(args.categories), (args.end - args.start).days + 1, len(todo), hours, MIN_INTERVAL_S)
    if hours > 24 and not args.yes:
        try:
            answer = input(f"This run needs at least {hours / 24:.1f} days of requests. Continue? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            log.info("Aborted. Narrow --categories or --start/--end, or pass --yes.")
            return 1

    client = ArxivClient(email=args.email)
    try:
        crawl(engine, client, todo)
    except KeyboardInterrupt:
        log.info("Interrupted - progress is saved; rerun the same command to resume.")
        return 130
    finally:
        engine.dispose()
    log.info("Done. %d API requests made.", client.requests_made)
    return 0


if __name__ == "__main__":
    sys.exit(main())
