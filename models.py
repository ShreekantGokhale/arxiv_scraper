"""SQLAlchemy 2.0 models for the arXiv paper database (MySQL 8.0.17+)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# All timestamps are stored as UTC. MySQL DATETIME has no time zone, so values are
# written as naive UTC; fsp=6 keeps microseconds, which the citation refresh logic relies on.
UtcDateTime = DATETIME(fsp=6)

# Binary collation for identifiers: exact, case-sensitive matching ("math.GT" != "math.gt").
ID_COLLATION = "utf8mb4_bin"

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4",
                 "mysql_collate": "utf8mb4_unicode_ci"}


def utcnow() -> datetime:
    """Current UTC time as a naive datetime, the form stored in DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Category(Base):
    """Reference table, seeded from taxonomy.py."""
    __tablename__ = "categories"
    __table_args__ = TABLE_OPTIONS

    code: Mapped[str] = mapped_column(String(32, collation=ID_COLLATION), primary_key=True)  # "cs.AI"
    name: Mapped[str | None] = mapped_column(String(255))          # "Artificial Intelligence"
    archive: Mapped[str] = mapped_column(String(32))               # "cs"
    group_name: Mapped[str | None] = mapped_column(String(128))    # "Computer Science"

    papers: Mapped[list["Paper"]] = relationship(back_populates="primary")


class Paper(Base):
    __tablename__ = "papers"
    __table_args__ = TABLE_OPTIONS

    arxiv_id: Mapped[str] = mapped_column(String(32, collation=ID_COLLATION), primary_key=True)
    latest_version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(JSON)               # JSON array, in author order
    primary_category: Mapped[str] = mapped_column(
        String(32, collation=ID_COLLATION), ForeignKey("categories.code"), index=True)
    other_categories: Mapped[list[str]] = mapped_column(JSON)      # JSON array, excludes primary
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime)    # v1 submission, UTC
    announced_date: Mapped[date] = mapped_column(Date, index=True)  # computed, US Eastern
    abstract: Mapped[str] = mapped_column(Text)
    abs_url: Mapped[str] = mapped_column(String(255))
    pdf_url: Mapped[str] = mapped_column(String(255))              # full-paper access link
    scraped_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    # Filled by enrich_citations.py from Semantic Scholar. All NULL = never looked up;
    # counts NULL but citations_updated_at set = Semantic Scholar doesn't know the paper (yet).
    citation_count: Mapped[int | None] = mapped_column(Integer, index=True)
    influential_citation_count: Mapped[int | None] = mapped_column(Integer)
    citations_updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)

    primary: Mapped[Category] = relationship(back_populates="papers")


class CrawlLog(Base):
    """One row per finished (category, submission day) query; makes runs resumable."""
    __tablename__ = "crawl_log"
    __table_args__ = TABLE_OPTIONS

    category: Mapped[str] = mapped_column(String(32, collation=ID_COLLATION), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    total_results: Mapped[int] = mapped_column(Integer)   # what the API said exists
    fetched: Mapped[int] = mapped_column(Integer)         # what we actually saved
    finished_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


# Indexes that create_all() won't add to an existing table.
# MySQL has no CREATE INDEX IF NOT EXISTS, so make_engine() checks each one first.
EXTRA_INDEXES = {
    # Full-text index for keyword search over titles and abstracts (used by the API).
    "ft_papers_title_abstract":
        "CREATE FULLTEXT INDEX ft_papers_title_abstract ON papers (title, abstract)",
}

# Indexes from earlier versions that make_engine() drops if present (dropping is instant).
# The multi-valued JSON index on other_categories can crash MySQL 8.4 whenever the table is
# rebuilt, e.g. when the full-text index is added (MySQL bug #121044). At this data size,
# cross-list lookups are fast without it.
LEGACY_INDEXES = ["ix_papers_other_categories"]
