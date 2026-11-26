"""Pydantic models: the JSON shape of every API response (also shown on /docs)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class PaperSummary(BaseModel):
    """A paper in a list: everything except the full abstract."""
    model_config = ConfigDict(from_attributes=True)

    arxiv_id: str = Field(examples=["2501.01234"])
    title: str
    authors: list[str]
    primary_category: str = Field(examples=["cs.CL"])
    other_categories: list[str] = Field(examples=[["cs.AI", "cs.LG"]])
    announced_date: date
    abstract_preview: str = Field(description="First ~300 characters of the abstract")
    citation_count: int | None = Field(description="NULL until enrich_citations.py has run")
    influential_citation_count: int | None
    citations_per_month: float | None = Field(
        description="citation_count / months since announcement (at least 1 month)")
    abs_url: str
    pdf_url: str


class PaperDetail(BaseModel):
    """One paper with all stored fields."""
    model_config = ConfigDict(from_attributes=True)

    arxiv_id: str
    latest_version: int
    title: str
    authors: list[str]
    primary_category: str
    primary_category_name: str | None
    other_categories: list[str]
    submitted_at: datetime = Field(description="Version-1 submission time, UTC")
    announced_date: date
    abstract: str
    citation_count: int | None
    influential_citation_count: int | None
    citations_per_month: float | None
    citations_updated_at: datetime | None = Field(description="When citations were looked up, UTC")
    abs_url: str
    pdf_url: str


class PaperPage(BaseModel):
    total: int = Field(description="Papers matching the filters, across all pages")
    page: int
    page_size: int
    pages: int
    items: list[PaperSummary]


class PopularPaper(PaperSummary):
    rank: int
    score: float = Field(description="Value of the chosen ranking metric")


class CategoryCount(BaseModel):
    code: str = Field(examples=["cs.CL"])
    name: str | None = Field(examples=["Computation and Language"])
    group_name: str | None = Field(examples=["Computer Science"])
    primary_papers: int = Field(description="Papers whose main category is this one")
    cross_listed_papers: int = Field(description="Papers listing it as an additional category")
    total_papers: int


class DailyCount(BaseModel):
    date: date
    papers: int


class DailyStats(BaseModel):
    total: int
    days: list[DailyCount]


class Topic(BaseModel):
    key: str = Field(examples=["rag"])
    label: str = Field(examples=["Retrieval-augmented generation"])
    query: str = Field(description="MySQL full-text expression the topic expands to")
