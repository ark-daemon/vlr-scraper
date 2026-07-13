# vlr-scraper

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-beta-orange.svg)](CHANGELOG.md)

Async SQLite warehouse for **Valorant esports pages on [VLR.gg](https://www.vlr.gg)** — queue-driven crawl, defensive HTML parsers, JSON/CSV/Parquet export.

**Fleet:** [hltv-scraper](https://github.com/ark-daemon/hltv-scraper) · [dota2-scraper](https://github.com/ark-daemon/dota2-scraper) · [rocket-league-scraper](https://github.com/ark-daemon/rocket-league-scraper) · [lol-esports-scraper](https://github.com/ark-daemon/lol-esports-scraper)

---

## What it does

Turns public VLR.gg HTML into a local relational dataset: events, matches, map scorelines, player/agent stats, economy and round logs, teams, rosters, rankings, and career aggregates. Designed as offline analytics infrastructure — not a hosted API and not affiliated with VLR.gg or Riot Games.

Maturity: **beta (`0.1.0`)**. Parsers tolerate missing fields; site layout changes will still break extraction until fixtures are updated.

---

## Architecture

```
seed / scrape flags
        │
        ▼
┌───────────────────┐     claim batch      ┌────────────────────┐
│  crawl_queue      │ ──────────────────► │  worker pool       │
│  (SQLite, WAL)    │                      │  (asyncio + sem)  │
└───────────────────┘                      └─────────┬──────────┘
        ▲                                            │
        │ mark done/fail                             ▼
        │                                 ┌────────────────────┐
        │                                 │  AsyncScraper.get  │
        │                                 │  httpx (primary)   │
        │                                 │  CloakBrowser if   │
        │                                 │  CF challenge/403  │
        │                                 └─────────┬──────────┘
        │                                           │ HTML
        │                                           ▼
        │                                 selectolax parsers
        │                                 (overview / perf /
        │                                  economy / logs …)
        │                                           │
        └───────────────────────────────────────────┘
                              rows → aiosqlite tables
                              export → JSON | CSV | Parquet*
```

\*Parquet requires the optional `export` extra (`pandas` + `pyarrow`).

**Resilience (as implemented in `base.py`):**

| Mechanism | Behavior |
|-----------|----------|
| **Token-bucket rate limit** | Default `RATE_LIMIT_RPS=0.67` (~1.5s between acquires) |
| **Retries** | Up to `RETRY_MAX` with exponential backoff + jitter |
| **Circuit breaker** | After 3 consecutive failures, pause ~180s × 1.5^trips (global, not per-URL) |
| **CloakBrowser fallback** | Used when HTML looks like a Cloudflare challenge or HTTP 403; session cookies/UA can be reused by httpx |

There is no proxy pool and no multi-browser rotation beyond session refresh.

---

## Quickstart

```bash
git clone https://github.com/ark-daemon/vlr-scraper.git
cd vlr-scraper

python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -e ".[dev,export]"
# CloakBrowser pulls a Chromium build on first browser fallback;
# playwright is a declared dependency of that stack.

cp .env.example .env
# set USER_AGENT to something with a real contact

vlr-scraper --help
vlr-scraper scrape --events          # seed queue from region event lists
vlr-scraper scrape --matches         # drain pending match queue items
vlr-scraper status                   # crawl_queue buckets
vlr-scraper stats                    # table row counts
vlr-scraper export --all --format json --output ./exports
```

Single-target paths:

```bash
vlr-scraper scrape --match-id 608990
vlr-scraper scrape-link --url "https://www.vlr.gg/event/2747/..."
```

Full crawl (long-running):

```bash
vlr-scraper scrape --all
```

---

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
| `USER_AGENT` | `vlr-scraper/1.0 (research)` | Default UA if browser session has none |
| `BASE_URL` | `https://www.vlr.gg` | Site origin |
| `LOG_LEVEL` | `INFO` | Loguru level |
| `ERRORS_LOG` | `errors.log` | Error sink path |
| `CLOUDFLARE_COOLDOWN_MINUTES` | `10` | CF cooldown floor |
| `CLOUDFLARE_COOLDOWN_MAX_MINUTES` | `120` | CF cooldown cap |
| `CLOAKBROWSER_HEADLESS` | `true` | Browser headless flag |
| `CLOAKBROWSER_HUMANIZE` | `false` | Humanize interactions |
| `CLOAKBROWSER_WAIT_SECONDS` | `30` | Browser wait budget |
| `CLOAKBROWSER_SESSION_PATH` | `browser_session.json` | Cookie/session file (gitignored) |

Hardcoded domain constants (not env): VLR listing regions, ranking regions, agent/map canonicalization lists.

---

## Data model + sample output

**Core tables** (see `vlr_scraper/schema.sql`):  
`events`, `teams`, `players`, `team_rosters`, `matches`, `maps_played`, `match_player_stats`, `match_performance`, `kill_matrix`, `match_economy`, `match_economy_rounds`, `match_logs`, `player_career_stats`, `team_map_stats`, `global_player_stats`, `rankings`, plus `crawl_queue` and `scrape_runs`.

Exportable set matches `EXPORTABLE_TABLES` in `exporter.py` (queue/run tables are operational, not exported).

**Sample rows** (from a partial local crawl — shape is real; completeness varies by scrape depth):

```json
// events
{"event_id": 2747, "name": "Liga BRAZA: 2026", "status": "completed"}
{"event_id": 2634, "name": "Momentum Gaming: Detonation Series"}

// teams
{"team_id": 2, "name": "Sentinels", "country": "United States"}
{"team_id": 14, "name": "T1", "country": "South Korea"}

// players
{"player_id": 4, "ign": "crashies", "country": "UNITED STATES", "current_team_id": 2593}
```

```json
// matches (queued / listing stage — scores often null until match detail completes)
{"match_id": 608990, "event_id": 2839, "status": "upcoming",
 "url": "https://www.vlr.gg/608990/karalaget-vs-wack-good-game-ligaen-2026-winter-division-1-r1"}
```

---

## Current limitations

- **Layout fragility.** Parsers are selector-based against VLR HTML; redesigns break fields silently (nulls) more often than hard crashes.
- **Two-stage data quality.** Event/match *discovery* can leave `team*_score` null until queue workers finish match detail pages.
- **Cloudflare dependency.** Heavy runs may trip bot checks; CloakBrowser fallback is slower and not guaranteed.
- **No official API.** ToS / robots compliance is the operator’s responsibility.
- **`beautifulsoup4` is declared** but parsing is selectolax-first; BS helpers are vestigial.
- **Parquet** needs `pip install -e ".[export]"`.
- **Tests** cover helpers, some fixture HTML, DB queue semantics, and smoke packaging — not full live-site integration.

---

## Tech stack

| Layer | Actually used |
|-------|----------------|
| Runtime | Python ≥3.11, asyncio |
| CLI | typer, rich |
| Config | pydantic-settings |
| HTTP | httpx |
| Browser fallback | cloakbrowser (`launch_async`); playwright is a transitive/declared dep for that stack |
| HTML | selectolax |
| Storage | aiosqlite (WAL) |
| Logging | loguru |
| Export | stdlib JSON/CSV; pandas + pyarrow optional for Parquet |
| Quality | pytest, ruff (dev) |

---

## License

MIT © ark-daemon — see [LICENSE](LICENSE).

See also [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md).
