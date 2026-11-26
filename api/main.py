"""
arXiv Papers API (FastAPI).

Run from the project folder:
    python -m uvicorn api.main:app --reload
then open http://127.0.0.1:8000/docs to try every endpoint in the browser.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from math import ceil
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.dialects.mysql import match
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas import (CategoryCount, DailyCount, DailyStats, PaperDetail, PaperPage,
                         PaperSummary, PopularPaper, Topic)
from api.search import TOPICS, TopicName, build_boolean_query
from models import Category, Paper
from taxonomy import expand

app = FastAPI(
    title="arXiv Papers API",
    version="1.0.0",
    description=(
        "Search and rank arXiv papers scraped into MySQL by `arxiv_scraper.py`, with "
        "citation counts from Semantic Scholar (`enrich_citations.py`).\n\n"
        "**Search tips:** `q` requires every word; use quotes for phrases "
        '(`"knowledge graph"`). Words shorter than 3 letters (like *AI*) only work '
        'inside a phrase (`"generative AI"`). `topic` gives ready-made searches for '
        "research topics such as RAG or GenAI, which aren't arXiv categories."
    ),
)

# citations / months since announcement, with at least one month so new papers aren't inflated
CITATIONS_PER_MONTH = (
    Paper.citation_count
    / func.greatest(1, func.datediff(func.curdate(), Paper.announced_date) / 30.44)
).label("citations_per_month")

PREVIEW_CHARS = 300


@app.exception_handler(DBAPIError)
def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
    """Turn the common database problems into clear 503 messages instead of a bare 500."""
    code = exc.orig.args[0] if exc.orig is not None and exc.orig.args else None
    if code == 1191:  # Can't find FULLTEXT index matching the column list
        detail = ("Keyword search index is missing. Restart the API (or run arxiv_scraper.py) "
                  "so it can create ft_papers_title_abstract.")
    elif code in (2003, 2006, 2013):  # can't connect / server gone away / lost connection
        detail = "Cannot reach MySQL. Is the MySQL service running?"
    else:
        return JSONResponse(status_code=500, content={"detail": "Database error"})
    return JSONResponse(status_code=503, content={"detail": detail})


# --------------------------------------------------------------------------
# Shared filters
# --------------------------------------------------------------------------
class PaperFilters:
    """Query parameters shared by /papers, /papers/popular and /stats/daily."""

    def __init__(
        self,
        category: Annotated[Optional[list[str]], Query(
            description="Category codes (`cs.CL`) or archives (`cs`). Repeat the parameter "
                        "or separate with commas: `cs.CL,cs.AI`. Matches main category and "
                        "cross-lists unless `primary_only` is true.")] = None,
        primary_only: Annotated[bool, Query(
            description="Only match papers whose MAIN category is one of `category`")] = False,
        start_date: Annotated[Optional[date], Query(description="Announced on or after")] = None,
        end_date: Annotated[Optional[date], Query(description="Announced on or before")] = None,
        q: Annotated[Optional[str], Query(
            max_length=200,
            description='Keywords in title or abstract; all must match. Phrases in quotes.')] = None,
        topic: Annotated[Optional[TopicName], Query(
            description="Ready-made topic search (see GET /topics)")] = None,
    ):
        self.categories: list[str] = []
        if category:
            selectors = [c.strip() for item in category for c in item.split(",") if c.strip()]
            try:
                self.categories = expand(selectors)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        if start_date and end_date and start_date > end_date:
            raise HTTPException(status_code=400, detail="start_date must not be after end_date")
        self.primary_only = primary_only
        self.start_date, self.end_date = start_date, end_date

        self.q_match: Optional[ColumnElement] = None
        if q is not None:
            boolean_q = build_boolean_query(q)
            if boolean_q is None:
                raise HTTPException(status_code=400, detail=(
                    "Nothing searchable in q: words need 3+ letters and can't be common words "
                    'like "the"; put short words in a phrase, e.g. "generative AI".'))
            self.q_match = match(Paper.title, Paper.abstract, against=boolean_q).in_boolean_mode()
        self.topic_match: Optional[ColumnElement] = None
        if topic is not None:
            self.topic_match = match(Paper.title, Paper.abstract,
                                     against=TOPICS[topic.value]["query"]).in_boolean_mode()

    def conditions(self) -> list[ColumnElement]:
        conds: list[ColumnElement] = []
        if self.categories:
            in_category = [Paper.primary_category.in_(self.categories)]
            if not self.primary_only:  # is c in the other_categories JSON array?
                in_category += [func.json_contains(Paper.other_categories, func.json_quote(c))
                                for c in self.categories]
            conds.append(or_(*in_category))
        if self.start_date:
            conds.append(Paper.announced_date >= self.start_date)
        if self.end_date:
            conds.append(Paper.announced_date <= self.end_date)
        if self.q_match is not None:
            conds.append(self.q_match)
        if self.topic_match is not None:
            conds.append(self.topic_match)
        return conds


def _preview(abstract: str) -> str:
    if len(abstract) <= PREVIEW_CHARS:
        return abstract
    return abstract[:PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"


def _cpm(value) -> Optional[float]:
    return None if value is None else round(float(value), 3)


def _summary(paper: Paper, cpm) -> dict:
    return {
        "arxiv_id": paper.arxiv_id, "title": paper.title, "authors": paper.authors,
        "primary_category": paper.primary_category, "other_categories": paper.other_categories,
        "announced_date": paper.announced_date, "abstract_preview": _preview(paper.abstract),
        "citation_count": paper.citation_count,
        "influential_citation_count": paper.influential_citation_count,
        "citations_per_month": _cpm(cpm), "abs_url": paper.abs_url, "pdf_url": paper.pdf_url,
    }


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
class SortOrder(str, Enum):
    relevance = "relevance"
    newest = "newest"
    oldest = "oldest"
    most_cited = "most_cited"
    most_influential = "most_influential"
    citations_per_month = "citations_per_month"


@app.get("/papers", response_model=PaperPage, tags=["papers"],
         summary="List papers with filters, keyword search, sorting and pages")
def list_papers(
    filters: Annotated[PaperFilters, Depends()],
    db: Annotated[Session, Depends(get_db)],
    min_citations: Annotated[Optional[int], Query(ge=0, description="At least this many citations")] = None,
    sort: Annotated[Optional[SortOrder], Query(
        description="Default: `relevance` when `q` is given, otherwise `newest`")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    conds = filters.conditions()
    if min_citations is not None:
        conds.append(Paper.citation_count >= min_citations)

    sort = sort or (SortOrder.relevance if filters.q_match is not None else SortOrder.newest)
    if sort is SortOrder.relevance and filters.q_match is None:
        raise HTTPException(status_code=400, detail="sort=relevance needs a q search")
    order_by = {
        SortOrder.relevance: [filters.q_match.desc() if filters.q_match is not None else None],
        SortOrder.newest: [Paper.announced_date.desc()],
        SortOrder.oldest: [Paper.announced_date.asc()],
        SortOrder.most_cited: [Paper.citation_count.desc()],         # NULLs sort last
        SortOrder.most_influential: [Paper.influential_citation_count.desc()],
        SortOrder.citations_per_month: [CITATIONS_PER_MONTH.desc()],
    }[sort] + [Paper.arxiv_id.desc()]  # stable order across pages

    total = db.scalar(select(func.count()).select_from(Paper).where(*conds))
    rows = db.execute(
        select(Paper, CITATIONS_PER_MONTH).where(*conds).order_by(*order_by)
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return PaperPage(
        total=total, page=page, page_size=page_size, pages=ceil(total / page_size),
        items=[PaperSummary(**_summary(p, cpm)) for p, cpm in rows],
    )


class PopularityMetric(str, Enum):
    citations_per_month = "citations_per_month"
    citations = "citations"
    influential = "influential"


@app.get("/papers/popular", response_model=list[PopularPaper], tags=["papers"],
         summary="Most popular papers by citations")
def popular_papers(
    filters: Annotated[PaperFilters, Depends()],
    db: Annotated[Session, Depends(get_db)],
    metric: Annotated[PopularityMetric, Query(
        description="`citations_per_month` (default) is fair to newer papers; `citations` is "
                    "the raw total; `influential` counts citations that built on the paper")]
        = PopularityMetric.citations_per_month,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    score = {
        PopularityMetric.citations_per_month: CITATIONS_PER_MONTH,
        PopularityMetric.citations: Paper.citation_count,
        PopularityMetric.influential: Paper.influential_citation_count,
    }[metric]
    conds = filters.conditions() + [Paper.citation_count.is_not(None)]
    if metric is PopularityMetric.influential:
        conds.append(Paper.influential_citation_count.is_not(None))
    rows = db.execute(
        select(Paper, CITATIONS_PER_MONTH, score.label("score")).where(*conds)
        .order_by(score.desc(), Paper.citation_count.desc(), Paper.arxiv_id).limit(limit)
    ).all()
    return [PopularPaper(**_summary(p, cpm), rank=i, score=round(float(s), 3))
            for i, (p, cpm, s) in enumerate(rows, 1)]


@app.get("/papers/{arxiv_id:path}", response_model=PaperDetail, tags=["papers"],
         summary="One paper with its full abstract",
         responses={404: {"description": "No paper with this arXiv ID in the database"}})
def get_paper(
    arxiv_id: Annotated[str, Path(description="arXiv ID without version, e.g. 2501.01234",
                                  examples=["2501.01234"])],
    db: Annotated[Session, Depends(get_db)],
):
    row = db.execute(
        select(Paper, CITATIONS_PER_MONTH, Category.name)
        .join(Category, Category.code == Paper.primary_category)
        .where(Paper.arxiv_id == arxiv_id.removesuffix("/"))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Paper {arxiv_id!r} not found")
    paper, cpm, category_name = row
    return PaperDetail(
        arxiv_id=paper.arxiv_id, latest_version=paper.latest_version, title=paper.title,
        authors=paper.authors, primary_category=paper.primary_category,
        primary_category_name=category_name, other_categories=paper.other_categories,
        submitted_at=paper.submitted_at, announced_date=paper.announced_date,
        abstract=paper.abstract, citation_count=paper.citation_count,
        influential_citation_count=paper.influential_citation_count,
        citations_per_month=_cpm(cpm), citations_updated_at=paper.citations_updated_at,
        abs_url=paper.abs_url, pdf_url=paper.pdf_url,
    )


@app.get("/categories", response_model=list[CategoryCount], tags=["categories"],
         summary="Categories with paper counts")
def list_categories(
    db: Annotated[Session, Depends(get_db)],
    include_empty: Annotated[bool, Query(description="Also list categories with no papers")] = False,
):
    primary = dict(db.execute(
        select(Paper.primary_category, func.count()).group_by(Paper.primary_category)).all())
    # Unnest the JSON arrays to count cross-lists per category in one query.
    cross = dict(db.execute(text(
        "SELECT jt.cat, COUNT(*) FROM papers, "
        "JSON_TABLE(papers.other_categories, '$[*]' COLUMNS (cat VARCHAR(32) PATH '$')) AS jt "
        "GROUP BY jt.cat")).all())
    result = []
    for cat in db.scalars(select(Category)):
        p, c = primary.get(cat.code, 0), cross.get(cat.code, 0)
        if p or c or include_empty:
            result.append(CategoryCount(code=cat.code, name=cat.name, group_name=cat.group_name,
                                        primary_papers=p, cross_listed_papers=c, total_papers=p + c))
    return sorted(result, key=lambda r: (-r.total_papers, r.code))


@app.get("/stats/daily", response_model=DailyStats, tags=["statistics"],
         summary="Papers announced per day (for charts)")
def daily_stats(
    filters: Annotated[PaperFilters, Depends()],
    db: Annotated[Session, Depends(get_db)],
):
    rows = db.execute(
        select(Paper.announced_date, func.count()).where(*filters.conditions())
        .group_by(Paper.announced_date).order_by(Paper.announced_date)
    ).all()
    return DailyStats(total=sum(n for _, n in rows),
                      days=[DailyCount(date=d, papers=n) for d, n in rows])


@app.get("/topics", response_model=list[Topic], tags=["topics"],
         summary="Topic shortcuts usable as ?topic=… on /papers, /papers/popular, /stats/daily")
def list_topics():
    return [Topic(key=k, label=v["label"], query=v["query"]) for k, v in TOPICS.items()]


@app.get("/health", tags=["health"], summary="Check the API and database are reachable")
def health(db: Annotated[Session, Depends(get_db)]):
    return {"status": "ok", "papers": db.scalar(select(func.count()).select_from(Paper))}
