# Structural Web Data Extraction Engine

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A production-grade, asynchronous pipeline for extracting structured DOM data from dynamic web targets. Built around a queue-based crawl architecture with resilient error handling, circuit-breaker patterns, and a configurable export layer.

---

## Architecture Overview

### Queue-Based Async Crawl Pipeline
The engine operates on a persistent task queue with atomic claim semantics. Each discovery phase seeds the queue; worker coroutines process items with configurable concurrency. A shared SQLite backend in WAL mode enables safe concurrent reads and writes across the pipeline.

### Structural DOM Parsing
Data extraction is decoupled from transport. Dedicated parser modules consume raw HTML and emit normalized, typed records. The parser layer is tolerant of minor layout shifts through fallback selectors and defensive null-handling.

### Dynamic Error Resilience
- **Circuit Breaker** — automatically throttles request velocity when upstream signals rate-limiting or transient failures.
- **Exponential Backoff Retry** — per-request retry with jitter and cooldown windows.
- **Session Rotation & Header Management** — runtime user-agent and cookie refresh without hardcoded credentials.
- **Graceful Degradation** — parser failures on individual nodes do not terminate the entire batch.

### Data Pipeline Output
Extracted records are persisted to a local relational store and can be exported on demand to:
- **JSON** — human-readable, schema-adjacent dumps
- **CSV** — flat-tabular consumption
- **Parquet** — columnar format for analytics workloads

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| HTTP / Browser | `httpx`, `playwright` |
| DOM Parsing | `selectolax`, `beautifulsoup4` |
| Storage | `aiosqlite` (SQLite + WAL) |
| CLI & Observability | `typer`, `rich`, `loguru` |
| Configuration | `pydantic-settings` |
| Quality | `pytest`, `ruff` |

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env to tune rate limits, concurrency, and output paths
```

### 3. Initialize database and run full extraction

```bash
python main.py scrape --all
```

### 4. Check pipeline status

```bash
python main.py status
python main.py stats
```

### 5. Export results

```bash
python main.py export --all --format json
python main.py export --all --format csv
python main.py export --all --format parquet
```

---

## Configuration

All runtime behavior is controlled through environment variables (loaded from `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `vlr_scraper.db` | SQLite database file |
| `RATE_LIMIT_RPS` | `0.67` | Max requests per second |
| `CONCURRENCY` | `3` | Concurrent worker coroutines |
| `RETRY_MAX` | `5` | Retry attempts per request |
| `RETRY_BACKOFF_BASE` | `2.0` | Exponential backoff multiplier |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout in seconds |
| `USER_AGENT` | `vlr-scraper/1.0 (research)` | Request user-agent string |
| `BASE_URL` | `https://www.vlr.gg` | Target base URL |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `CLOAKBROWSER_HEADLESS` | `true` | Headless browser mode |
| `CLOAKBROWSER_HUMANIZE` | `false` | Human-like interaction patterns |
| `CLOAKBROWSER_WAIT_SECONDS` | `30` | Browser initialization wait |
| `CLOAKBROWSER_SESSION_PATH` | `browser_session.json` | Session persistence file |

> **Security note:** `.env` and session files are listed in `.gitignore` and must never be committed.

---

## Project Structure

```
.
├── main.py                 # CLI entrypoint and orchestration
├── base.py                 # Async HTTP client, circuit breaker, retry logic
├── connection.py           # SQLite connection manager (WAL mode)
├── schema.sql              # Relational schema for extracted entities
├── config.py               # Pydantic settings loader
├── queries.py              # Async CRUD and queue operations
├── exporter.py             # JSON / CSV / Parquet export layer
├── events.py               # Target discovery and event indexing
├── matches.py              # Match-page orchestration dispatcher
├── match_overview.py       # Scoreboard and player-stat parsers
├── match_performance.py    # Multi-kill and clutch parsing
├── match_economy.py        # Economic round-state extraction
├── match_logs.py           # Round-by-round event-log parser
├── teams.py                # Team roster and map-stat parsers
├── players.py              # Player bio and career-stat parsers
├── stats.py                # Aggregate leaderboard extraction
├── rankings.py             # Regional ranking extraction
├── parser_helpers.py       # Shared text-cleaning and ID-extraction utilities
├── rate_limiter.py         # Token-bucket rate limiter
├── tests/
│   └── test_parsers.py     # Unit tests for parsers, DB layer, and rate limiter
├── pyproject.toml          # PEP 621 project metadata
├── requirements.txt        # Runtime dependencies
└── README.md               # This file
```

---

## Testing

```bash
python -m pytest tests/ -v
```

The test suite covers:
- Text sanitization and numeric parsing utilities
- DOM node extraction against minimal fixture HTML
- Database upsert, queue claim, and crash-recovery semantics
- Token-bucket rate-limiter refill correctness

---

## License

MIT © 2026

---

> **Disclaimer:** This tool is intended for legitimate data research, archival, and analytics. Users are responsible for complying with the target platform's Terms of Service, rate-limiting policies, and applicable data-protection regulations. The authors provide no warranty and assume no liability for misuse.
