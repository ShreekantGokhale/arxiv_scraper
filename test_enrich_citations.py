"""Offline tests for enrich_citations.py (DB tests need TEST_DATABASE_URL; see conftest.py)."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import arxiv_scraper as s
import enrich_citations as e
from conftest import DB_URL, needs_db
from models import Paper
from test_arxiv_scraper import ENTRY_A, ENTRY_B, ENTRY_C, feed


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code, self._payload, self.headers, self.text = status, payload, headers or {}, text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, lookup=None):
        """Either a fixed list of responses, or a dict arxiv_id -> hit used to answer any batch."""
        self.responses, self.lookup, self.calls, self.headers = list(responses or []), lookup, [], {}

    def post(self, url, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": params, "ids": json["ids"]})
        if self.lookup is not None:
            return FakeResponse(200, [self.lookup.get(i.removeprefix("ARXIV:")) for i in json["ids"]])
        return self.responses.pop(0)


def hit(cites, influential):
    return {"paperId": "x", "citationCount": cites, "influentialCitationCount": influential}


# ---- client ---------------------------------------------------------------
def test_client_request_shape_and_api_key():
    session = FakeSession([FakeResponse(200, [hit(10, 2), None])])
    client = e.SemanticScholarClient(api_key="KEY", min_interval=0, session=session)
    assert client.citations(["2403.01234", "cs/0101001"]) == [hit(10, 2), None]
    call = session.calls[0]
    assert call["url"] == "https://api.semanticscholar.org/graph/v1/paper/batch"
    assert call["ids"] == ["ARXIV:2403.01234", "ARXIV:cs/0101001"]
    assert call["params"] == {"fields": "citationCount,influentialCitationCount"}
    assert session.headers["x-api-key"] == "KEY"


def test_default_intervals_depend_on_key():
    assert e.SemanticScholarClient(api_key="K", session=FakeSession()).min_interval == e.INTERVAL_WITH_KEY_S
    no_key = e.SemanticScholarClient(session=FakeSession())
    assert no_key.min_interval == e.INTERVAL_WITHOUT_KEY_S
    assert "x-api-key" not in no_key.session.headers


def test_client_retries_then_succeeds():
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(503),
                           FakeResponse(200, [hit(1, 0)])])
    client = e.SemanticScholarClient(min_interval=0, session=session)
    assert client.citations(["2403.01234"]) == [hit(1, 0)]
    assert client.requests_made == 3


def test_client_raises_on_bad_request_and_bad_shape():
    client = e.SemanticScholarClient(min_interval=0, session=FakeSession([
        FakeResponse(400, text='{"error":"bad ids"}'), FakeResponse(200, [hit(1, 0)])]))
    with pytest.raises(e.SemanticScholarError, match="HTTP 400"):
        client.citations(["a"])
    with pytest.raises(e.SemanticScholarError, match="shape"):
        client.citations(["a", "b"])  # 2 IDs in, 1 result out
    with pytest.raises(ValueError):
        client.citations(["x"] * 501)


# ---- database -------------------------------------------------------------
LOOKUP = {"2403.01234": hit(120, 15), "2403.04444": hit(3, 0)}  # 2403.05555 unknown to S2


def load_papers(engine):
    with Session(engine) as db, db.begin():
        s.save_papers(db, s.parse_feed(feed(3, 0, ENTRY_A, ENTRY_B, ENTRY_C))[1])


@needs_db
def test_enrich_fills_columns_in_batches(engine, monkeypatch):
    monkeypatch.setattr(e, "BATCH_SIZE", 2)
    load_papers(engine)
    session = FakeSession(lookup=LOOKUP)
    stats = e.enrich(engine, e.SemanticScholarClient(min_interval=0, session=session))

    assert stats == {"processed": 3, "found": 2, "not_found": 1}
    assert [len(c["ids"]) for c in session.calls] == [2, 1]
    with Session(engine) as db:
        a, b = db.get(Paper, "2403.01234"), db.get(Paper, "2403.05555")
        assert (a.citation_count, a.influential_citation_count) == (120, 15)
        assert a.citations_updated_at is not None
        assert b.citation_count is None and b.citations_updated_at is not None  # looked up, unknown

    # Nothing is due right after a run...
    again = FakeSession(lookup=LOOKUP)
    assert e.enrich(engine, e.SemanticScholarClient(min_interval=0, session=again))["processed"] == 0
    assert again.calls == []
    # ...but --refresh-days 0 re-checks everything exactly once (no endless loop).
    stats = e.enrich(engine, e.SemanticScholarClient(min_interval=0, session=FakeSession(lookup=LOOKUP)),
                     refresh_days=0)
    assert stats["processed"] == 3


@needs_db
def test_enrich_category_filter_and_limit(engine):
    load_papers(engine)
    session = FakeSession(lookup=LOOKUP)
    client = e.SemanticScholarClient(min_interval=0, session=session)
    # cs.RO appears only as a cross-list of 2403.04444
    assert e.enrich(engine, client, categories=["cs.RO"])["processed"] == 1
    assert session.calls[0]["ids"] == ["ARXIV:2403.04444"]
    assert e.enrich(engine, client, limit=1)["processed"] == 1
    with Session(engine) as db:
        done = db.scalars(select(Paper.arxiv_id).where(Paper.citations_updated_at.isnot(None))).all()
        assert len(done) == 2


@needs_db
def test_scraper_upsert_keeps_citation_counts(engine):
    load_papers(engine)
    e.enrich(engine, e.SemanticScholarClient(min_interval=0, session=FakeSession(lookup=LOOKUP)))
    with Session(engine) as db, db.begin():  # arXiv scraper sees a new version of the paper
        s.save_papers(db, s.parse_feed(feed(1, 0, ENTRY_A.replace("v2<", "v3<")))[1])
    with Session(engine) as db:
        a = db.get(Paper, "2403.01234")
        assert a.latest_version == 3 and a.citation_count == 120


@needs_db
def test_mysql_schema_details(engine):
    with engine.connect() as conn:
        idx = conn.execute(text(
            "SELECT index_name, expression FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'papers' "
            "AND index_name = 'ix_papers_other_categories'")).all()
        assert len(idx) == 1 and "array" in idx[0][1].lower()        # multi-valued index
        charset = conn.scalar(text(
            "SELECT table_collation FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'papers'"))
        assert charset.startswith("utf8mb4")
        precision = conn.scalar(text(
            "SELECT datetime_precision FROM information_schema.columns WHERE table_schema = DATABASE() "
            "AND table_name = 'papers' AND column_name = 'citations_updated_at'"))
        assert precision == 6
    s.make_engine(DB_URL).dispose()  # running setup twice is harmless (index already exists)


@needs_db
def test_unicode_titles_and_authors_round_trip(engine):
    entry = (ENTRY_A.replace("2403.01234", "2403.07777")
             .replace("Asha Rao", "Zoë Müller-Łukasik").replace("Retrieval-Augmented", "Rétrieval 𝔼[x] 漢字"))
    with Session(engine) as db, db.begin():
        s.save_papers(db, s.parse_feed(feed(1, 0, entry))[1])
    with Session(engine) as db:
        p = db.get(Paper, "2403.07777")
        assert p.authors[0] == "Zoë Müller-Łukasik" and "𝔼[x] 漢字" in p.title


def test_rejects_non_mysql_url():
    with pytest.raises(ValueError, match="MySQL"):
        s.make_engine("sqlite://")


def test_database_url_from_env_escapes_password(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_NAME", "arxiv")
    monkeypatch.setenv("DB_USER", "me")
    monkeypatch.setenv("DB_PASSWORD", "p@ss:w/rd#1")
    url = s.database_url_from_env()
    assert url.password == "p@ss:w/rd#1" and url.port == 3306 and url.host == "localhost"
    assert "p%40ss%3Aw%2Frd%231" in url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://x@h/db")
    assert s.database_url_from_env() == "mysql+pymysql://x@h/db"
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("DB_NAME")
    assert s.database_url_from_env() is None


def test_cli_validation():
    with pytest.raises(SystemExit):
        e.parse_args(["--db-url", "mysql+pymysql://x", "--categories", "cs.NOPE"])
    args = e.parse_args(["--db-url", "mysql+pymysql://x", "--categories", "stat", "--refresh-days", "7"])
    assert args.categories[0] == "stat.AP" and args.refresh_days == 7
