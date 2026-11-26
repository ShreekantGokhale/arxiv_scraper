"""
Tests for the FastAPI app (api/). Most need TEST_DATABASE_URL (see conftest.py);
the search-parser and validation tests run without a database.
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

import arxiv_scraper as s
from api.db import get_db
from api.main import app
from api.search import build_boolean_query
from conftest import needs_db
from models import Paper

client = TestClient(app)


# ---- search parser (no database) -----------------------------------------
@pytest.mark.parametrize("user_query, expected", [
    ('rag "knowledge graph" medical', '+rag* +"knowledge graph" +medical*'),
    ("retrieval-augmented agents", '+"retrieval augmented" +agents*'),
    ('"generative AI" for the classroom', '+"generative AI" +classroom*'),  # stopwords dropped
    ("AI", None),                               # too short outside a phrase
    ("the of", None),                           # only stopwords
    ("+evil -(syntax) ~attack*", "+evil* +syntax* +attack*"),  # operators stripped
    ("LLM LLM", "+LLM*"),                       # duplicates removed
    ('"chain-of-thought" reasoning', '+"chain of thought" +reasoning*'),
])
def test_build_boolean_query(user_query, expected):
    assert build_boolean_query(user_query) == expected


@pytest.fixture
def no_db():
    """Validation errors must fire before the database is touched."""
    app.dependency_overrides[get_db] = lambda: iter([None])
    yield
    app.dependency_overrides.clear()


def test_topics_endpoint():
    topics = client.get("/topics").json()
    assert {t["key"] for t in topics} >= {"rag", "llm", "genai", "agents", "human_ai"}


@pytest.mark.parametrize("url, message", [
    ("/papers?category=cs.NOPE", "Unknown arXiv category"),
    ("/papers?start_date=2025-06-01&end_date=2025-01-01", "start_date"),
    ("/papers?q=AI", "Nothing searchable"),
    ("/papers?sort=relevance", "needs a q"),
])
def test_validation_errors(no_db, url, message):
    r = client.get(url)
    assert r.status_code == 400 and message in r.json()["detail"]


def test_database_errors_become_clear_503s():
    from pymysql.err import OperationalError
    from sqlalchemy.exc import OperationalError as SAOperationalError

    def failing(code, message):
        def dependency():
            class Broken:
                def scalar(self, *a, **k):
                    raise SAOperationalError("SELECT ...", {}, OperationalError(code, message))
                execute = scalar
            yield Broken()
        return dependency

    app.dependency_overrides[get_db] = failing(2003, "Can't connect to MySQL server")
    r = client.get("/health")
    assert r.status_code == 503 and "MySQL service running" in r.json()["detail"]
    app.dependency_overrides[get_db] = failing(1191, "Can't find FULLTEXT index")
    r = client.get("/papers?q=retrieval")
    assert r.status_code == 503 and "search index is missing" in r.json()["detail"]
    app.dependency_overrides.clear()


def test_parameter_limits(no_db):
    assert client.get("/papers?page_size=101").status_code == 422
    assert client.get("/papers?topic=astrology").status_code == 422
    assert client.get("/papers/popular?metric=likes").status_code == 422


# ---- database-backed tests -------------------------------------------------
def rec(arxiv_id, title, abstract, primary, others, announced, authors=("A. Author",)):
    return s.PaperRecord(
        arxiv_id=arxiv_id, latest_version=1, title=title, authors=list(authors),
        primary_category=primary, other_categories=list(others),
        submitted_at=datetime.combine(announced, datetime.min.time()), announced_date=announced,
        abstract=abstract, abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}")


PAPERS = [
    rec("2501.00001", "Retrieval-Augmented Generation for Medical QA",
        "We build a RAG pipeline over clinical guidelines. " + "Details. " * 60,
        "cs.CL", ["cs.AI", "cs.IR"], date(2025, 1, 6), authors=("Asha Rao", "Ben Lee")),
    rec("2502.00002", "Large Language Models as Tutors",
        "A user study of large language models in classrooms.",
        "cs.HC", ["cs.CL"], date(2025, 2, 10)),
    rec("2503.00003", "Diffusion Models for Text-to-Image Generation",
        "Denoising diffusion for high-resolution image generation.",
        "cs.CV", ["cs.LG"], date(2025, 3, 3)),
    rec("2504.00004", "Multi-Agent Reinforcement Learning at Scale",
        "Cooperative multi-agent policies with graph networks.",
        "cs.MA", ["cs.LG", "cs.AI"], date(2025, 4, 7)),
    rec("2505.00005", "Sharper Generalization Bounds",
        "Theory of generalization for overparameterized networks.",
        "cs.LG", ["stat.ML"], date(2025, 5, 5)),
    rec("2506.00006", "Uncited Graph Paper", "Graph neural networks without citations yet.",
        "cs.LG", [], date(2025, 6, 2)),
    rec("math/0601001", "An Old-Style Identifier", "Legacy identifier formats still resolve.",
        "math.CO", [], date(2025, 1, 6)),
]
CITATIONS = {  # arxiv_id: (citations, influential)
    "2501.00001": (120, 15), "2502.00002": (40, 2), "2503.00003": (300, 30),
    "2504.00004": (10, 1), "2505.00005": (0, 0),
}


@pytest.fixture
def api(engine):
    with Session(engine) as db, db.begin():
        s.save_papers(db, PAPERS)
        db.execute(update(Paper), [
            {"arxiv_id": k, "citation_count": c, "influential_citation_count": i,
             "citations_updated_at": datetime(2026, 11, 1)} for k, (c, i) in CITATIONS.items()])

    def override():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_db] = override
    yield client
    app.dependency_overrides.clear()


def ids(response):
    return [p["arxiv_id"] for p in response.json()["items"]]


@needs_db
def test_list_default_newest_and_pagination(api):
    r = api.get("/papers", params={"page_size": 3})
    body = r.json()
    assert r.status_code == 200 and body["total"] == 7 and body["pages"] == 3
    assert ids(r) == ["2506.00006", "2505.00005", "2504.00004"]
    assert ids(api.get("/papers", params={"page_size": 3, "page": 3})) == ["2501.00001"]
    first = api.get("/papers", params={"category": "cs.CL", "primary_only": True}).json()["items"][0]
    assert first["authors"] == ["Asha Rao", "Ben Lee"]
    assert first["abstract_preview"].endswith("…") and len(first["abstract_preview"]) <= 301


@needs_db
def test_category_filters(api):
    # cs.CL as main category OR cross-list
    assert sorted(ids(api.get("/papers?category=cs.CL"))) == ["2501.00001", "2502.00002"]
    assert ids(api.get("/papers?category=cs.CL&primary_only=true")) == ["2501.00001"]
    # repeated and comma-separated parameters are equivalent
    a = ids(api.get("/papers?category=cs.MA&category=cs.CV"))
    b = ids(api.get("/papers?category=cs.MA,cs.CV"))
    assert sorted(a) == sorted(b) == ["2503.00003", "2504.00004"]
    assert api.get("/papers?category=math").json()["total"] == 1  # archive expands


@needs_db
def test_date_range(api):
    r = api.get("/papers?start_date=2025-02-01&end_date=2025-04-30&sort=oldest")
    assert ids(r) == ["2502.00002", "2503.00003", "2504.00004"]


@needs_db
def test_keyword_search(api):
    assert ids(api.get("/papers", params={"q": "retrieval-augmented"})) == ["2501.00001"]
    assert ids(api.get("/papers", params={"q": '"large language models" classroom'})) == ["2502.00002"]
    assert ids(api.get("/papers", params={"q": "diffusion image"})) == ["2503.00003"]
    assert api.get("/papers", params={"q": "diffusion medical"}).json()["total"] == 0  # all words required
    graph = api.get("/papers", params={"q": "graph"})  # matches 'graph' and 'graphs'
    assert sorted(ids(graph)) == ["2504.00004", "2506.00006"]


@needs_db
def test_topics(api):
    assert ids(api.get("/papers?topic=rag")) == ["2501.00001"]
    assert ids(api.get("/papers?topic=genai")) == ["2503.00003"]
    assert ids(api.get("/papers?topic=agents")) == ["2504.00004"]
    assert ids(api.get("/papers?topic=human_ai")) == ["2502.00002"]
    assert sorted(ids(api.get("/papers?topic=llm"))) == ["2502.00002"]
    assert api.get("/papers?topic=rag&category=cs.CV").json()["total"] == 0  # filters combine


@needs_db
def test_sorting_and_min_citations(api):
    assert ids(api.get("/papers?sort=most_cited&page_size=3")) == ["2503.00003", "2501.00001", "2502.00002"]
    r = api.get("/papers?sort=most_cited")
    assert [p["citation_count"] for p in r.json()["items"]][-2:] == [None, None]  # NULLs last
    assert ids(api.get("/papers?min_citations=100&sort=oldest")) == ["2501.00001", "2503.00003"]
    top = api.get("/papers?sort=citations_per_month&page_size=1").json()["items"][0]
    assert top["arxiv_id"] == "2503.00003" and top["citations_per_month"] > 0


@needs_db
def test_popular(api):
    r = api.get("/papers/popular?limit=3")
    body = r.json()
    assert [p["arxiv_id"] for p in body] == ["2503.00003", "2501.00001", "2502.00002"]
    assert [p["rank"] for p in body] == [1, 2, 3] and body[0]["score"] == body[0]["citations_per_month"]
    infl = api.get("/papers/popular?metric=influential&category=cs.AI").json()
    assert [(p["arxiv_id"], p["score"]) for p in infl] == [("2501.00001", 15.0), ("2504.00004", 1.0)]
    assert [p["arxiv_id"] for p in api.get("/papers/popular?topic=llm").json()] == ["2502.00002"]


@needs_db
def test_paper_detail_and_404(api):
    r = api.get("/papers/2501.00001")
    p = r.json()
    assert r.status_code == 200 and p["primary_category_name"] == "Computation and Language"
    assert p["abstract"].startswith("We build a RAG pipeline") and p["citation_count"] == 120
    assert p["other_categories"] == ["cs.AI", "cs.IR"] and p["citations_updated_at"].startswith("2026-11-01")
    assert api.get("/papers/math/0601001").json()["title"] == "An Old-Style Identifier"
    assert api.get("/papers/9999.99999").status_code == 404


@needs_db
def test_categories(api):
    cats = {c["code"]: c for c in api.get("/categories").json()}
    assert cats["cs.LG"]["primary_papers"] == 2 and cats["cs.LG"]["cross_listed_papers"] == 2
    assert cats["cs.AI"] == {"code": "cs.AI", "name": "Artificial Intelligence",
                             "group_name": "Computer Science", "primary_papers": 0,
                             "cross_listed_papers": 2, "total_papers": 2}
    assert "cs.RO" not in cats
    assert len(api.get("/categories?include_empty=true").json()) == 155


@needs_db
def test_daily_stats_and_health(api):
    r = api.get("/stats/daily?category=cs.LG")
    assert r.json() == {"total": 4, "days": [
        {"date": "2025-03-03", "papers": 1}, {"date": "2025-04-07", "papers": 1},
        {"date": "2025-05-05", "papers": 1}, {"date": "2025-06-02", "papers": 1}]}
    assert api.get("/stats/daily?topic=rag").json()["total"] == 1
    assert api.get("/health").json() == {"status": "ok", "papers": 7}
