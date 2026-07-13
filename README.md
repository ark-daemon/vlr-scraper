# VLR Scraper — Valorant Esports Data Pipeline

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Async Python 3.11+ scraper for **Valorant esports data from [VLR.gg](https://www.vlr.gg)** — match results, player stats, team rankings, event placements, agent picks, and round-by-round logs.

Data is stored in a local SQLite warehouse and can be exported to JSON, CSV, or Parquet.

---

## Install

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -e ".[dev,export]"
python -m playwright install chromium
```

Copy environment defaults:

```bash
cp .env.example .env
# Edit USER_AGENT with a real contact email before heavy runs.
```

## Usage

```bash
vlr-scraper scrape --all
vlr-scraper status
vlr-scraper export --all --format json
```

Or:

```bash
python -m vlr_scraper scrape --all
```

## Configuration

All settings load from `.env` (see `.env.example`). Important defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `vlr_scraper.db` | SQLite database path |
| `RATE_LIMIT_RPS` | `0.67` | Max requests per second (~1.5s between requests) |
| `CONCURRENCY` | `3` | Concurrent workers |
| `USER_AGENT` | `vlr-scraper/1.0 (research)` | Request identity — set a real contact |
| `BASE_URL` | `https://www.vlr.gg` | Target site |

## Project layout

```
vlr_scraper/          # Installable Python package
  main.py             # CLI (typer)
  base.py             # HTTP client, retries, circuit breaker
  schema.sql          # SQLite schema
  ...
tests/
pyproject.toml
```

## Testing

```bash
pytest -q
```

## Responsible use

- Intended for research, archival, and personal analytics.
- Keep rate limits conservative. Do not hammer VLR.gg.
- Users are responsible for complying with VLR.gg Terms of Service, robots.txt, and applicable law.
- This project is **not** affiliated with VLR.gg or Riot Games.

## License

MIT © 2026 ark-daemon
