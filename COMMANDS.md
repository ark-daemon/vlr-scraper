# `vlr-scraper`

VLR.gg <span style="font-weight: bold">Valorant</span> esports scraper — queue crawl to SQLite.

**Usage**:

```console
$ vlr-scraper [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `scrape`: Run the queue-based VLR.gg crawl pipeline.
* `scrape-link`: Scrape from specific VLR link: - Event URL...
* `update`: Update the database with new matches and...
* `export`: Export database tables to JSON, CSV, or...
* `status`: Show crawl queue progress counts.
* `queue`: Show detailed crawl queue dashboard.
* `stats`: Show row counts for all database tables.

## `vlr-scraper scrape`

Run the queue-based VLR.gg crawl pipeline.

**Usage**:

```console
$ vlr-scraper scrape [OPTIONS]
```

**Options**:

* `--all`: Full crawl
* `--events`: Events only
* `--matches`: Pending matches
* `--matches-only`: Process only queued matches (skip events/teams/players)
* `--teams`: Pending teams
* `--players`: Pending players
* `--stats`: Global stats
* `--match-id INTEGER`: Single match ID
* `--event-id INTEGER`: Single event ID
* `--force-rescrape`: Re-scrape done items
* `--retry-failed`: Retry only failed items
* `--dry-run`: Fetch/plan without writing scraper results
* `--help`: Show this message and exit.

## `vlr-scraper scrape-link`

Scrape from specific VLR link:
- Event URL -&gt; event page -&gt; all event matches -&gt; teams -&gt; players
- Match URL -&gt; that match -&gt; teams -&gt; players

**Usage**:

```console
$ vlr-scraper scrape-link [OPTIONS]
```

**Options**:

* `--url TEXT`: VLR event or match URL  [required]
* `--force-rescrape`: Re-scrape done queue items for relevant phases
* `--help`: Show this message and exit.

## `vlr-scraper update`

Update the database with new matches and events.

**Usage**:

```console
$ vlr-scraper update [OPTIONS]
```

**Options**:

* `--new-only`: Only process new queue items
* `--since INTEGER`: Rescrape event/match queue items updated in last N days
* `--help`: Show this message and exit.

## `vlr-scraper export`

Export database tables to JSON, CSV, or Parquet.

**Usage**:

```console
$ vlr-scraper export [OPTIONS]
```

**Options**:

* `--table TEXT`: Table name to export
* `--all`: Export all tables
* `--format TEXT`: Output format: json|csv|parquet  [default: json]
* `--output TEXT`: Output directory  [default: ./exports/]
* `--help`: Show this message and exit.

## `vlr-scraper status`

Show crawl queue progress counts.

**Usage**:

```console
$ vlr-scraper status [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

## `vlr-scraper queue`

Show detailed crawl queue dashboard.

**Usage**:

```console
$ vlr-scraper queue [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

## `vlr-scraper stats`

Show row counts for all database tables.

**Usage**:

```console
$ vlr-scraper stats [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.
