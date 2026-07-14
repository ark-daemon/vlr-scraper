# VLR Scraper

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-beta-orange.svg)](CHANGELOG.md)

> Async SQLite warehouse for [VLR.gg](https://www.vlr.gg) Valorant esports pages -- queue-driven crawl, defensive HTML parsers, table export, and fleet match snapshots.

**Fleet:** [hltv-scraper](https://github.com/ark-daemon/hltv-scraper) · [dota2-scraper](https://github.com/ark-daemon/dota2-scraper) · [rocket-league-scraper](https://github.com/ark-daemon/rocket-league-scraper) · [lol-esports-scraper](https://github.com/ark-daemon/lol-esports-scraper)

## Features

- **Queue-driven crawl** -- SQLite `crawl_queue` + concurrent workers with resume after interrupt
- **httpx first, browser fallback** -- CloakBrowser only when Cloudflare/403 blocks plain HTTP
- **Deep match data** -- maps, player/agent stats, economy, kill matrix, round logs
- **Entities** -- events, teams, rosters, players, rankings, career aggregates
- **Table export** -- JSON / CSV / Parquet (Parquet needs the `export` extra)
- **Fleet snapshot** -- match-grain `export/` (`data.json` + `csv` + `parquet` + `manifest.json`)
- **Optional R2 publish** -- overwrite-in-place upload with public manifest verification

Maturity: **beta (`0.1.0`)**. Offline analytics tooling -- not a hosted API and not affiliated with VLR.gg or Riot Games.

## Getting started

```bash
git clone https://github.com/ark-daemon/vlr-scraper.git
cd vlr-scraper

python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -e ".[dev,export]"
cp .env.example .env
# set USER_AGENT to something with a real contact

vlr-scraper --help
```

CloakBrowser downloads Chromium on first browser fallback. Playwright is a declared dependency of that stack.

## Usage

```bash
# Seed and crawl
vlr-scraper scrape --events          # region event lists -> queue
vlr-scraper scrape --matches         # drain pending match items
vlr-scraper scrape --all             # full crawl (long-running)
vlr-scraper status                   # crawl_queue buckets
vlr-scraper stats                    # table row counts

# Single targets
vlr-scraper scrape --match-id 608990
vlr-scraper scrape-link --url "https://www.vlr.gg/event/2747/..."

# Table dump (exports/)
vlr-scraper export --all --format json --output ./exports

# Fleet match snapshot (export/)
vlr-scraper snapshot
vlr-scraper snapshot --publish       # write + upload to R2
vlr-scraper publish                  # upload existing export/
```

Full Typer-generated CLI docs: [COMMANDS.md](COMMANDS.md).

## Architecture

```
seed / scrape flags
        |
        v
 crawl_queue (SQLite, WAL)  --->  worker pool (asyncio + sem)
        ^                                   |
        | mark done/fail                    v
        |                         AsyncScraper.get
        |                         httpx (primary)
        |                         CloakBrowser if CF / 403
        |                                   |
        |                                   v
        |                         selectolax parsers
        +-----------------------------------+
                    rows -> aiosqlite
                    export  -> exports/  (tables)
                    snapshot -> export/  (match grain)
```

| Mechanism | Behavior |
|-----------|----------|
| Token-bucket rate limit | Default `RATE_LIMIT_RPS=0.67` (~1.5s between acquires) |
| Retries | Up to `RETRY_MAX` with exponential backoff + jitter |
| Circuit breaker | After 3 consecutive failures, pause ~180s * 1.5^trips (global) |
| CloakBrowser fallback | On Cloudflare challenge HTML or HTTP 403; session cookies/UA can be reused by httpx |

No proxy pool and no multi-browser rotation beyond session refresh.

## Configuration

Loaded from environment / `.env` via `pydantic-settings` (`vlr_scraper/config.py`). No prefix.

| Variable | Default | Role |
|----------|---------|------|
| `DB_PATH` | `vlr_scraper.db` | SQLite file |
| `RATE_LIMIT_RPS` | `0.67` | Token-bucket rate |
| `CONCURRENCY` | `3` | Concurrent queue workers |
| `RETRY_MAX` | `5` | Per-URL attempts |
| `RETRY_BACKOFF_BASE` | `2.0` | Exponential backoff base |
| `REQUEST_TIMEOUT` | `30` | httpx timeout (seconds) |
| `USER_AGENT` | `vlr-scraper/1.0 (research)` | Default UA |
| `BASE_URL` | `https://www.vlr.gg` | Site origin |
| `LOG_LEVEL` | `INFO` | Loguru level |
| `ERRORS_LOG` | `errors.log` | Error sink path |
| `CLOUDFLARE_COOLDOWN_MINUTES` | `10` | CF cooldown floor |
| `CLOUDFLARE_COOLDOWN_MAX_MINUTES` | `120` | CF cooldown cap |
| `CLOAKBROWSER_HEADLESS` | `true` | Browser headless flag |
| `CLOAKBROWSER_HUMANIZE` | `false` | Humanize interactions |
| `CLOAKBROWSER_WAIT_SECONDS` | `30` | Browser wait budget |
| `CLOAKBROWSER_SESSION_PATH` | `browser_session.json` | Cookie/session file (gitignored) |

**R2 publish** (optional, for `snapshot --publish` / `publish`):

| Variable | Role |
|----------|------|
| `R2_ACCOUNT_ID` | Cloudflare account id |
| `R2_ACCESS_KEY_ID` | R2 API token access key |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_BUCKET` | Bucket name |
| `R2_PUBLIC_BASE_URL` | Public base, no trailing slash (e.g. `https://pub-xxx.r2.dev`) |

Objects land at `{base}/vlr/{data.json,data.csv,data.parquet,manifest.json}`.

## Data model

Core tables (`vlr_scraper/schema.sql`):
`events`, `teams`, `players`, `team_rosters`, `matches`, `maps_played`, `match_player_stats`, `match_performance`, `kill_matrix`, `match_economy`, `match_economy_rounds`, `match_logs`, `player_career_stats`, `team_map_stats`, `global_player_stats`, `rankings`, plus operational `crawl_queue` and `scrape_runs`.

Sample shape (partial crawl; completeness depends on depth):

```json
{"event_id": 2747, "name": "Liga BRAZA: 2026", "status": "completed"}
{"team_id": 2, "name": "Sentinels", "country": "United States"}
{"match_id": 608990, "event_id": 2839, "status": "upcoming",
 "url": "https://www.vlr.gg/608990/..."}
```

### Fleet snapshot (`export/`)

Match/series grain, `schema_version` **1.0**:

| Column | Notes |
|--------|--------|
| `match_id` | Fleet-prefixed id (`vlr:...`) |
| `match_date` | `YYYY-MM-DD` or null |
| `team_a` / `team_b` | Display names |
| `winner` | Exact `team_a` / `team_b` or null |
| `source_url` | Canonical VLR URL |
| `status` | `scheduled` / `live` / `completed` (+ canceled/postponed when known) |
| `score_a` / `score_b` | Nullable ints |
| `event_name`, `format`, `raw_status` | Context |

> [!NOTE]
> `export/` is the **snapshot** bundle. Table dumps go to `exports/` via `export`.

## Limitations

> [!WARNING]
> Site layout changes break selectors silently (null fields) more often than hard crashes. ToS / robots compliance is the operator's responsibility.

- **Two-stage quality** -- listing stage can leave scores null until match detail workers finish
- **Cloudflare friction** -- heavy runs may need browser fallback (slower, not guaranteed)
- **Parquet** needs `pip install -e ".[export]"`
- **Tests** cover helpers, fixture HTML, queue semantics, and packaging smoke -- not live-site integration

## Tech stack

| Layer | Used |
|-------|------|
| Runtime | Python >=3.11, asyncio |
| CLI | typer + rich (`vlr-scraper`) |
| Config | pydantic-settings |
| HTTP | httpx |
| Browser fallback | cloakbrowser (+ playwright for that stack) |
| HTML | selectolax |
| Storage | aiosqlite (WAL) |
| Logging | loguru + rich CLI chrome |
| Export | stdlib JSON/CSV; pandas + pyarrow optional for Parquet |
| Publish | boto3 optional at runtime (`pip install boto3`) |
| Quality | pytest, ruff (dev) |
