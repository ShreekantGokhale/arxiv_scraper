# arXiv Metadata Scraper (MySQL + SQLAlchemy)

Builds a MySQL database of arXiv papers (title, authors, main category, other
categories, announcement date, abstract, full-paper link) by harvesting the
**official arXiv API** one *(category, submission day)* pair at a time.
A second step adds **citation counts from Semantic Scholar**, and a **FastAPI**
service lets you search, filter and rank the papers over HTTP.

## Files

| File | Purpose |
|---|---|
| `arxiv_scraper.py` | Step 1: HTTP client, Atom parser, crawl loop, CLI |
| `enrich_citations.py` | Step 2: Semantic Scholar citation lookup, 500 papers per request |
| `api/main.py` | Step 3: FastAPI app and all endpoints |
| `api/search.py` | Keyword-search parser and topic shortcuts (RAG, GenAI, …) |
| `api/schemas.py` | Pydantic response models (the JSON shapes shown on `/docs`) |
| `api/db.py` | Database session for each API request |
| `models.py` | SQLAlchemy 2.0 models: `Category`, `Paper`, `CrawlLog` |
| `taxonomy.py` | All 155 arXiv categories, grouped by archive and group |
| `.env.example` | Template for your database settings and API key |
| `conftest.py` | Shared test fixtures (throwaway MySQL schema, no sleeping) |
| `test_arxiv_scraper.py` | Tests: parser, retries, pagination, DB upserts, resume |
| `test_enrich_citations.py` | Tests: batch lookups, retries, refresh logic, MySQL schema details |
| `test_api.py` | Tests: every endpoint, filters, search, sorting, validation |

## Requirements

- **Python 3.10 or newer**
- **MySQL 8.0 or newer.** The scraper uses JSON columns and a FULLTEXT index.

## Setup

```bash
pip install -r requirements.txt
mysql -u root -p -e "CREATE DATABASE arxiv CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
cp .env.example .env                             # then fill in your MySQL password
```

Both scripts read the `.env` file automatically. The tables are created on the
first run, and `categories` is seeded from `taxonomy.py`.

## Usage

```bash
python arxiv_scraper.py --list-categories        # show all 155 categories

# Smoke test: 2 categories x 1 week
python arxiv_scraper.py --email you@gmail.com \
    --categories cs.CL cs.LG --start 2024-03-04 --end 2024-03-10

# 4 years of four ML categories (~5,800 requests, ~5 hours)
python arxiv_scraper.py --email you@gmail.com \
    --categories cs.AI cs.CL cs.CV cs.LG --start 2022-11-21 --end 2026-11-20
```

`--categories` accepts category codes (`cs.AI`), whole archives (`cs`,
`astro-ph`, `cond-mat`) or `all`. `--order day` finishes all categories for one
day before moving to the next; the default (`category`) finishes one category
before starting the next. Ctrl+C is safe: rerun the same command and it resumes.

### How long will it take?

Every (category, day) pair costs at least one request, and requests are spaced
3 seconds apart:

| Selection | Pairs for 4 years | Minimum time |
|---|---|---|
| 1 category | 1,461 | ~1.2 h |
| 4 categories | 5,844 | ~4.9 h |
| `cs` (40 categories) | 58,440 | ~2 days |
| `all` (155 categories) | 226,455 | ~7.9 days |

Runs longer than 24 hours ask for confirmation (skip it with `--yes`).

### Step 2: citation counts

```bash
python enrich_citations.py --limit 1000           # try it on a small slice first
python enrich_citations.py                        # every paper that is due
python enrich_citations.py --categories cs.CL     # only papers listed in cs.CL
```

The script looks up papers that were never checked, or were last checked more
than `--refresh-days` ago (default 30). Rerun it monthly to keep counts current.
It sends one request per 500 papers, so 100,000 papers take about 200 requests:
roughly 4 minutes with a key (`S2_API_KEY` in `.env`), or 10 without one.

### Step 3: the API

```bash
python -m uvicorn api.main:app --reload
```

Then open **http://127.0.0.1:8000/docs**: an interactive page where you can try
every endpoint. `--reload` restarts the server when you edit the code; press
Ctrl+C to stop it.

| Endpoint | Returns |
|---|---|
| `GET /papers` | Paper list with filters, keyword search, sorting and pages |
| `GET /papers/{arxiv_id}` | One paper with its full abstract |
| `GET /papers/popular` | Top papers by citations per month, total citations or influential citations |
| `GET /categories` | Categories with main and cross-listed paper counts |
| `GET /stats/daily` | Papers announced per day (for charts) |
| `GET /topics` | Topic shortcuts usable as `?topic=` |
| `GET /health` | API and database check |

`/papers`, `/papers/popular` and `/stats/daily` share these filters:

| Parameter | Example | Meaning |
|---|---|---|
| `category` | `cs.CL` or `cs.CL,cs.HC` or `cs` | Main category or cross-list (archives expand) |
| `primary_only` | `true` | Only match the main category |
| `start_date`, `end_date` | `2025-01-01` | Announcement date range |
| `q` | `"knowledge graph" medical` | Keywords in title/abstract; all must match; quotes for phrases |
| `topic` | `rag`, `llm`, `genai`, `agents`, `prompting`, `hallucination`, `human_ai` | Ready-made topic search |

Example requests:

```
/papers?topic=rag&category=cs.CL&sort=most_cited
/papers?q=retrieval-augmented agents&start_date=2025-03-01&end_date=2025-04-30
/papers/popular?topic=genai&metric=citations_per_month&limit=10
/papers/popular?category=cs.HC&metric=influential
/stats/daily?topic=llm&category=cs.AI
/papers/2501.01234
```

**How search works.** Keyword search uses a MySQL FULLTEXT index on title and
abstract, which the scraper creates automatically. Raw input is rebuilt into a
safe boolean-mode query, because MySQL has some traps:
- **Every word is required**, and words also match their plurals (`agent` finds `agents`).
- **Hyphens:** `retrieval-augmented` is searched as a phrase. Unquoted, MySQL would read the `-` as NOT.
- **Short words and stopwords** (`AI`, `the`) are ignored outside quotes, because MySQL doesn't index them. Put them in a phrase: `"generative AI"`.
- **Operator characters** in user input are removed.

GenAI and RAG are research topics, not arXiv categories, so `?topic=` expands
to a set of phrases (see `GET /topics` or `api/search.py`, where you can add
your own topics).

## Schema

**`papers`** (InnoDB, utf8mb4)

| Column | MySQL type | Notes |
|---|---|---|
| `arxiv_id` | VARCHAR(32) PK | e.g. `2401.01234`, no version suffix |
| `latest_version` | INT | from the `vN` suffix |
| `title` | TEXT | whitespace normalised |
| `authors` | JSON | array of names, in author order |
| `primary_category` | VARCHAR(32) FK → `categories.code` | main category |
| `other_categories` | JSON | array of all other categories |
| `submitted_at` | DATETIME(6) | version-1 submission time, UTC |
| `announced_date` | DATE | computed from arXiv's schedule (below) |
| `abstract` | TEXT | the API's `<summary>` |
| `abs_url`, `pdf_url` | VARCHAR(255) | abstract page and full-paper link |
| `scraped_at` | DATETIME(6) | last time the arXiv scraper wrote this row, UTC |
| `citation_count` | INT, nullable | Semantic Scholar's total citations |
| `influential_citation_count` | INT, nullable | citations where the paper shaped the citing work |
| `citations_updated_at` | DATETIME(6), nullable | when citations were last looked up, UTC |

How to read the citation columns:
- **All three NULL:** never looked up.
- **Counts NULL but a timestamp set:** Semantic Scholar doesn't know the paper. This is common for papers from the last few days, which it hasn't indexed yet.
- **Rescraping doesn't touch them:** the arXiv scraper's upsert never overwrites the citation columns.

**`categories`**: `code`, `name`, `archive`, `group_name`. Seeded with all 155
categories. Legacy codes found in the data are added automatically.

**`crawl_log`**: PK `(category, day)`, plus `total_results` (what the API
reported), `fetched` (what was saved) and `finished_at`.

All timestamps are stored in UTC. MySQL's DATETIME has no time zone, so
remember this when comparing with local time.

## Design notes (useful for a project report)

**API, not website scraping.** arXiv forbids indiscriminate automated downloads
of its website and blocks offending IPs; the API is the sanctioned machine
interface. The scraper follows its Terms of Use: one request per 3 seconds,
a single connection, and a User-Agent header with a contact email.

**Per-category, per-day partitioning.** Each request is
`cat:<code> AND submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]`, paged 1,000
results at a time. A pair's papers and its `crawl_log` row commit in one
transaction, so a crash never leaves a half-saved pair marked as done. Adding
a category later only fetches the new pairs.

**Cross-listed papers.** A `cat:` query matches a category whether it is the
paper's primary category or a cross-list, so one paper can come back under
several queries. `INSERT … ON DUPLICATE KEY UPDATE` keeps exactly one row per
paper and refreshes it when a newer version appears.

**Lists in MySQL.** MySQL has no array type, so `authors` and
`other_categories` are JSON arrays, queried with `MEMBER OF` / `JSON_CONTAINS`.
A later normalisation step can move both into link tables (`paper_authors`,
`paper_categories`), which also makes cross-list lookups indexable.

An earlier version also put a *multi-valued index* on `other_categories`. It was
removed because MySQL 8.4 can crash while rebuilding such an index over arrays
that are a mix of empty and non-empty (MySQL bug #121044), and a rebuild happens
whenever the table is altered, e.g. when the FULLTEXT index is added. The scraper
drops it automatically from databases created by that version.

**Robustness.**
- Retries with exponential backoff on network errors and HTTP 5xx.
- Honours `Retry-After` on 429/503.
- Detects the plain-text "Rate exceeded" body that arXiv sometimes returns with HTTP 200.
- Retries transient empty pages and raises on the API's error entries.
- Leaves today's pairs unmarked, because submissions are still arriving.

**Announcement date.** arXiv doesn't expose it in the API, so it's computed
from the version-1 submission time (US Eastern):

| Submitted | Announced |
|---|---|
| Mon 14:00 – Tue 14:00 | Tue 20:00 |
| Tue 14:00 – Wed 14:00 | Wed 20:00 |
| Wed 14:00 – Thu 14:00 | Thu 20:00 |
| Thu 14:00 – Fri 14:00 | Sun 20:00 |
| Fri 14:00 – Mon 14:00 | Mon 20:00 |

The result is off only when arXiv defers an announcement (holidays) or
moderators put a paper on hold.

**Citations come from a second source.** arXiv publishes neither citation
counts nor per-paper download statistics, so `enrich_citations.py` looks up
each arXiv ID (as `ARXIV:<id>`) through Semantic Scholar's batch endpoint. It
has its own rate limiter:
- **Pacing:** 1.1 s between requests with an API key, 3 s without one.
- **Errors:** retries 429/5xx with backoff and honours `Retry-After`.
- **Progress:** each batch is committed as soon as it's looked up, so Ctrl+C loses nothing.

**Popularity is not the same as raw citations.** Older papers have had more
time to be cited, so rank by citations per month since announcement (see the
queries below). Influential citations are a stricter signal than the total.

## Example queries (MySQL)

```sql
-- Papers announced per day in cs.CL (primary)
SELECT announced_date, COUNT(*) FROM papers
WHERE primary_category = 'cs.CL' GROUP BY announced_date ORDER BY announced_date;

-- Papers cross-listed in cs.RO
SELECT title, primary_category FROM papers WHERE 'cs.RO' MEMBER OF (other_categories);

-- First author and author count
SELECT title, authors->>'$[0]' AS first_author, JSON_LENGTH(authors) AS n_authors
FROM papers LIMIT 10;

-- Daily volume per category as reported by the API (includes cross-lists)
SELECT category, day, total_results FROM crawl_log ORDER BY category, day;

-- Papers per group
SELECT c.group_name, COUNT(*) AS papers FROM papers p
JOIN categories c ON c.code = p.primary_category
GROUP BY c.group_name ORDER BY papers DESC;

-- Most popular cs.CL papers, fair to recent ones: citations per month since announcement
SELECT arxiv_id, title, citation_count,
       ROUND(citation_count / GREATEST(1, DATEDIFF(CURRENT_DATE, announced_date) / 30.44), 2)
         AS cites_per_month
FROM papers
WHERE primary_category = 'cs.CL' AND citation_count IS NOT NULL
ORDER BY cites_per_month DESC LIMIT 20;

-- Most influential papers announced in 2025
SELECT title, influential_citation_count, citation_count FROM papers
WHERE announced_date >= '2025-01-01' AND announced_date < '2026-01-01'
  AND influential_citation_count IS NOT NULL
ORDER BY influential_citation_count DESC LIMIT 20;

-- Citation coverage
SELECT SUM(citations_updated_at IS NULL)                              AS never_checked,
       SUM(citations_updated_at IS NOT NULL AND citation_count IS NULL) AS unknown_to_s2,
       COUNT(citation_count)                                          AS with_counts
FROM papers;
```

## Troubleshooting

**MySQL stops ("Lost connection to MySQL server during query", then "Can't connect")
when a script starts.** Adding the keyword-search (FULLTEXT) index to a `papers`
table that already holds rows makes MySQL rebuild the table, and that has crashed
the MySQL server itself (seen on MySQL 9.5 on Windows). The scripts now create the
index only while the table is empty, and otherwise log
`Not adding index(es) ft_papers_title_abstract ...`. If you see that warning, or
the API answers *"Keyword search index is missing"*, recreate the database:

1. Start MySQL again if it stopped (Windows: Services → MySQL → Start).
2. In the MySQL client:
   ```sql
   DROP DATABASE arxiv;
   CREATE DATABASE arxiv CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   ```
3. Run `arxiv_scraper.py` again. It creates the tables and the index while they
   are empty, and inserting papers afterwards is safe.

## Tests

```bash
python -m pytest -q                              # tests that need no database (37)
TEST_DATABASE_URL="mysql+pymysql://root:PASSWORD@localhost:3306/arxiv_test?charset=utf8mb4" \
    python -m pytest -q                          # + MySQL tests (56 total)
```

The database tests **drop and recreate** the tables in `TEST_DATABASE_URL`, so
point it at a throwaway database (create it like `arxiv` above). Special
characters in the password must be %-encoded in this URL, e.g. `@` → `%40`.
