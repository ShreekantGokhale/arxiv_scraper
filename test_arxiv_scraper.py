"""
Offline tests for arxiv_scraper.py - no network needed.

    python -m pytest -q

Database tests need a disposable MySQL database (they drop and recreate
its tables) and are skipped unless TEST_DATABASE_URL is set, e.g.
    TEST_DATABASE_URL=mysql+pymysql://root:PASSWORD@localhost:3306/arxiv_test
"""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

import arxiv_scraper as s
from conftest import needs_db
from models import Category, CrawlLog, Paper
from taxonomy import ARCHIVES, CATEGORIES, expand

FEED_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>arXiv Query</title>
  <opensearch:totalResults>{total}</opensearch:totalResults>
  <opensearch:startIndex>{start}</opensearch:startIndex>
"""

ENTRY_A = """
  <entry>
    <id>http://arxiv.org/abs/2403.01234v2</id>
    <updated>2024-03-20T10:00:00Z</updated>
    <published>2024-03-04T15:30:00Z</published>
    <title>Retrieval-Augmented
      Generation for Low-Resource Languages</title>
    <summary>  We study retrieval augmentation
      for low-resource languages.  </summary>
    <author><name>Asha Rao</name><arxiv:affiliation>IISc</arxiv:affiliation></author>
    <author><name>Ben Lee</name></author>
    <arxiv:comment>Accepted to ACL 2024. 9 pages</arxiv:comment>
    <link href="http://arxiv.org/abs/2403.01234v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2403.01234v2" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <category term="I.2.7" scheme="http://arxiv.org/schemas/atom"/>
  </entry>"""

ENTRY_B = """
  <entry>
    <id>http://arxiv.org/abs/2403.05555v1</id>
    <published>2024-03-08T20:00:00Z</published>
    <title>Sharper Bounds for SGD</title>
    <summary>Bounds.</summary>
    <author><name>C. Diaz</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/2403.05555v1" rel="related"/>
    <arxiv:primary_category term="cs.LG"/>
    <category term="cs.LG"/><category term="stat.ML"/><category term="68T05"/>
  </entry>"""

ENTRY_C = """
  <entry>
    <id>http://arxiv.org/abs/2403.04444v1</id>
    <published>2024-03-07T18:00:00Z</published>
    <title>Diffusion Policies</title>
    <summary>Policies.</summary>
    <author><name>D. Kim</name></author>
    <arxiv:primary_category term="cond-mat.soft"/>
    <category term="cond-mat.soft"/><category term="cs.RO"/>
  </entry>"""

ENTRY_LEGACY = """
  <entry>
    <id>http://arxiv.org/abs/2403.09999v1</id>
    <published>2024-03-04T12:00:00Z</published>
    <title>Old Category</title><summary>x</summary>
    <author><name>E. F.</name></author>
    <arxiv:primary_category term="cmp-lg"/><category term="cmp-lg"/><category term="cs.CL"/>
  </entry>"""

ERROR_FEED = FEED_HEAD.format(total=1, start=0) + """
  <entry><id>http://arxiv.org/api/errors#incorrect_id_format</id><title>Error</title>
  <summary>incorrect id format</summary></entry></feed>"""


def feed(total, start, *entries):
    return (FEED_HEAD.format(total=total, start=start) + "".join(entries) + "\n</feed>").encode()


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


def make_client(responses):
    return s.ArxivClient(email="test@example.com", min_interval=0, session=FakeSession(responses))


# ---- taxonomy -------------------------------------------------------------
def test_taxonomy_counts_and_expand():
    assert len(CATEGORIES) == 155
    assert len(ARCHIVES["cs"]) == 40
    assert CATEGORIES["cs.AI"] == ("Artificial Intelligence", "cs", "Computer Science")
    assert CATEGORIES["cond-mat.soft"][1:] == ("cond-mat", "Physics")
    assert expand(["cs.AI", "stat", "cs.AI"])[:2] == ["cs.AI", "stat.AP"]
    assert len(expand(["all"])) == 155
    with pytest.raises(ValueError):
        expand(["cs.XYZ"])


# ---- announcement dates (arXiv schedule, US Eastern) ----------------------
@pytest.mark.parametrize("utc, expected", [
    ("2024-03-04T15:30:00", "2024-03-04"),  # Mon 10:30 EST -> Mon (Fri14-Mon14 window)
    ("2024-03-04T19:30:00", "2024-03-05"),  # Mon 14:30 EST -> Tue
    ("2024-03-07T18:00:00", "2024-03-07"),  # Thu 13:00 EST -> Thu
    ("2024-03-07T20:00:00", "2024-03-10"),  # Thu 15:00 EST -> Sun
    ("2024-03-08T15:00:00", "2024-03-10"),  # Fri 10:00 EST -> Sun
    ("2024-03-08T20:00:00", "2024-03-11"),  # Fri 15:00 EST -> Mon
    ("2024-03-09T12:00:00", "2024-03-11"),  # Sat -> Mon
    ("2024-07-02T17:59:00", "2024-07-02"),  # Tue 13:59 EDT (summer time) -> Tue
    ("2024-07-02T18:00:00", "2024-07-03"),  # Tue 14:00 EDT -> Wed
])
def test_announcement_date(utc, expected):
    ts = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
    assert s.announcement_date(ts).isoformat() == expected


# ---- parsing --------------------------------------------------------------
def test_parse_entry_fields():
    total, records = s.parse_feed(feed(1, 0, ENTRY_A))
    r = records[0]
    assert total == 1
    assert r.arxiv_id == "2403.01234" and r.latest_version == 2
    assert r.title == "Retrieval-Augmented Generation for Low-Resource Languages"
    assert r.abstract == "We study retrieval augmentation for low-resource languages."
    assert r.authors == ["Asha Rao", "Ben Lee"]
    assert r.primary_category == "cs.CL"
    assert r.other_categories == ["cs.AI"]          # ACM code I.2.7 dropped
    assert r.submitted_at == datetime(2024, 3, 4, 15, 30)  # naive UTC
    assert r.announced_date == date(2024, 3, 4)
    assert r.pdf_url == "https://arxiv.org/pdf/2403.01234v2"
    assert r.abs_url == "https://arxiv.org/abs/2403.01234"
    assert not hasattr(r, "venue")


def test_hyphenated_categories_and_built_pdf_link():
    _, records = s.parse_feed(feed(1, 0, ENTRY_C))
    assert records[0].primary_category == "cond-mat.soft"
    assert records[0].other_categories == ["cs.RO"]
    assert records[0].pdf_url == "https://arxiv.org/pdf/2403.04444"


def test_api_error_entry_raises():
    with pytest.raises(s.ArxivAPIError, match="incorrect id format"):
        s.parse_feed(ERROR_FEED.encode())


# ---- HTTP client ----------------------------------------------------------
def test_retries_on_429_and_rate_exceeded_text():
    client = make_client([
        FakeResponse(429, b"", {"Retry-After": "5"}),
        FakeResponse(200, b"Rate exceeded."),
        FakeResponse(503),
        FakeResponse(200, feed(0, 0)),
    ])
    assert b"<feed" in client.get({"q": 1})
    assert client.requests_made == 4


def test_sets_user_agent_with_email():
    assert "mailto:test@example.com" in make_client([]).session.headers["User-Agent"]


def test_fetch_category_day_paginates_and_retries_empty_page(monkeypatch):
    monkeypatch.setattr(s, "PAGE_SIZE", 2)
    client = make_client([
        FakeResponse(200, feed(3, 0, ENTRY_A, ENTRY_C)),
        FakeResponse(200, feed(3, 2)),                 # transient empty page
        FakeResponse(200, feed(3, 2, ENTRY_B)),
    ])
    total, records = s.fetch_category_day(client, "cs.LG", date(2024, 3, 4))
    assert total == 3 and len(records) == 3
    calls = client.session.calls
    assert [c["start"] for c in calls] == [0, 2, 2]
    assert calls[0]["search_query"] == "cat:cs.LG AND submittedDate:[202403040000 TO 202403042359]"


# ---- MySQL (fixtures in conftest.py) --------------------------------------
@needs_db
def test_categories_seeded(engine):
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Category)) == 155
        assert db.get(Category, "cs.CV").name == "Computer Vision and Pattern Recognition"


@needs_db
def test_crawl_per_category_per_day_and_resume(engine):
    cats, start, end = ["cs.CL", "cs.LG"], date(2024, 3, 4), date(2024, 3, 5)
    todo = s.plan(engine, cats, start, end)
    assert todo == [("cs.CL", start), ("cs.CL", end), ("cs.LG", start), ("cs.LG", end)]

    client = make_client([
        FakeResponse(200, feed(2, 0, ENTRY_A, ENTRY_LEGACY)),  # cs.CL 03-04
        FakeResponse(200, feed(0, 0)),                         # cs.CL 03-05
        FakeResponse(200, feed(2, 0, ENTRY_B, ENTRY_A)),       # cs.LG 03-04 (A again: cross-list)
        FakeResponse(200, feed(1, 0, ENTRY_C)),                # cs.LG 03-05
    ])
    s.crawl(engine, client, todo)
    assert [c["search_query"].split(" AND ")[0] for c in client.session.calls] == \
        ["cat:cs.CL", "cat:cs.CL", "cat:cs.LG", "cat:cs.LG"]

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Paper)) == 4   # A stored once
        a = db.get(Paper, "2403.01234")
        assert a.authors == ["Asha Rao", "Ben Lee"]                     # native ARRAY
        assert a.other_categories == ["cs.AI"]
        assert a.announced_date == date(2024, 3, 4)
        assert a.primary.name == "Computation and Language"            # FK relationship
        assert db.get(Category, "cmp-lg").name is None                  # legacy code auto-added
        log = {(r.category, r.day): (r.total_results, r.fetched) for r in db.scalars(select(CrawlLog))}
        assert log[("cs.LG", start)] == (2, 2) and log[("cs.CL", end)] == (0, 0)
        # JSON array lookup
        hits = db.scalars(select(Paper.title).where(
            func.json_contains(Paper.other_categories, func.json_quote("cs.RO")))).all()
        assert hits == ["Diffusion Policies"]
        cols = {r[0] for r in db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'papers'"))}
        assert "venue" not in cols

    # Resume: everything is logged, so a rerun plans nothing.
    assert s.plan(engine, cats, start, end) == []
    # Adding a category plans only the new pairs.
    assert s.plan(engine, cats + ["stat.ML"], start, end) == [("stat.ML", start), ("stat.ML", end)]


@needs_db
def test_upsert_updates_new_version(engine):
    with Session(engine) as db, db.begin():
        s.save_papers(db, s.parse_feed(feed(1, 0, ENTRY_A))[1])
    with Session(engine) as db, db.begin():
        s.save_papers(db, s.parse_feed(feed(1, 0, ENTRY_A.replace("v2<", "v3<")))[1])
    with Session(engine) as db:
        assert db.get(Paper, "2403.01234").latest_version == 3
        assert db.scalar(select(func.count()).select_from(Paper)) == 1


@needs_db
def test_today_is_not_marked_done(engine):
    today = datetime.now(timezone.utc).date()
    s.crawl(engine, make_client([FakeResponse(200, feed(0, 0))]), [("cs.AI", today)])
    assert s.plan(engine, ["cs.AI"], today, today) == [("cs.AI", today)]
