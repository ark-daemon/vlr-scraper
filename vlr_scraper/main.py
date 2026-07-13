"""
VLR.gg Scraper €” Main CLI
Usage: python main.py --help
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from loguru import logger
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

import vlr_scraper.queries as queries
from vlr_scraper.base import AsyncScraper
from vlr_scraper.cli_ui import (
    configure_rich_logging,
    console,
    end_summary_table,
    startup_panel,
    status_table,
    timed_run,
)
from vlr_scraper.config import settings
from vlr_scraper.connection import execute_read, execute_write, init_db
from vlr_scraper.events import EventsScraper
from vlr_scraper.exporter import EXPORTABLE_TABLES, export_all, export_table
from vlr_scraper.matches import MatchScraper
from vlr_scraper.parser_helpers import extract_event_id, extract_match_id
from vlr_scraper.players import PlayerScraper
from vlr_scraper.rankings import RankingsScraper
from vlr_scraper.stats import StatsScraper
from vlr_scraper.teams import TeamScraper

app = typer.Typer(
    name="vlr-scraper",
    help="VLR.gg [bold]Valorant[/] esports scraper — queue crawl to SQLite.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)

# -----------------------------------------------------------------------
# Signal handling for graceful shutdown
# -----------------------------------------------------------------------
_shutdown_event = asyncio.Event()


def _handle_shutdown(sig, frame):
    logger.warning(f"Received signal {sig}. Initiating graceful shutdown...")
    _shutdown_event.set()


# -----------------------------------------------------------------------
# Crawl loop helpers
# -----------------------------------------------------------------------


async def _process_queue(
    page_type: str,
    scraper_fn,
    concurrency: int = settings.CONCURRENCY,
    force_rescrape: bool = False,
    retry_failed: bool = False,
) -> None:
    sem = asyncio.Semaphore(concurrency)
    run_id = await queries.scrape_run_start(f"queue:{page_type}")
    AsyncScraper.get_runtime_metrics(reset=True)

    if force_rescrape:
        logger.info(f"Force-rescrape: resetting {page_type} queue entries to pending.")
        await queries.queue_reset_for_page_type(page_type)
    elif retry_failed:
        count = await queries.queue_retry_failed_for_page_type(page_type)
        logger.info(f"Retry-failed: reset {count} failed {page_type} entries to pending.")

    processed = 0
    failed = 0
    batch_size = 10  # small batches to avoid huge task backlogs during rate limiting

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=2,
    ) as progress:
        task = progress.add_task(f"[cyan]Processing {page_type}...", total=None)

        while not _shutdown_event.is_set():
            pending = await queries.queue_claim_pending(page_type=page_type, limit=batch_size)
            if not pending:
                break

            tasks = []
            for row in pending:
                queue_id = row["queue_id"]
                url = row["url"]
                retries = row["retries"] or 0

                if retries >= settings.RETRY_MAX:
                    await queries.queue_mark_failed(queue_id, "Max retries exceeded")
                    failed += 1
                    continue

                async def _run(url=url, qid=queue_id):
                    nonlocal failed
                    async with sem:
                        if _shutdown_event.is_set():
                            return
                        try:
                            await scraper_fn(url, qid)
                        except Exception as exc:
                            import traceback as _tb

                            logger.error(
                                f"Unhandled error processing {url}: {exc}\n{_tb.format_exc()}"
                            )
                            await queries.queue_mark_failed(qid, str(exc))
                            failed += 1

                tasks.append(asyncio.create_task(_run()))

            if tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
                processed += len(done)
                progress.update(task, advance=len(done))

            # Breathe between batches to stay under VLR's rate limit radar
            await asyncio.sleep(1.0)

    counts = await queries.queue_counts()
    pending_count = counts.get("pending", 0) if isinstance(counts, dict) else 0
    console.print(
        f"\n[green]Done.[/green] Processed={processed} Failed={failed} Remaining={pending_count}"
    )
    metrics = AsyncScraper.get_runtime_metrics(reset=True)
    await queries.scrape_run_finish(
        run_id=run_id,
        status="completed",
        pages_processed=processed,
        pages_failed=failed,
        cloudflare_blocks=metrics["cloudflare_blocks"],
        cloakbrowser_successes=metrics["cloakbrowser_successes"],
    )


# -----------------------------------------------------------------------
# scrape command
# -----------------------------------------------------------------------


@app.command()
def scrape(
    scrape_all: bool = typer.Option(False, "--all", help="Full crawl"),
    events: bool = typer.Option(False, "--events", help="Events only"),
    matches: bool = typer.Option(False, "--matches", help="Pending matches"),
    matches_only: bool = typer.Option(
        False,
        "--matches-only",
        help="Process only queued matches (skip events/teams/players)",
    ),
    teams: bool = typer.Option(False, "--teams", help="Pending teams"),
    players: bool = typer.Option(False, "--players", help="Pending players"),
    stats: bool = typer.Option(False, "--stats", help="Global stats"),
    match_id: int | None = typer.Option(None, "--match-id", help="Single match ID"),
    event_id: int | None = typer.Option(None, "--event-id", help="Single event ID"),
    force_rescrape: bool = typer.Option(False, "--force-rescrape", help="Re-scrape done items"),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Retry only failed items"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Fetch/plan without writing scraper results"
    ),
) -> None:
    """Run the queue-based VLR.gg crawl pipeline."""
    _setup_logging()
    targets = []
    if scrape_all:
        targets.append("all")
    if events:
        targets.append("events")
    if matches:
        targets.append("matches")
    if matches_only:
        targets.append("matches-only")
    if teams:
        targets.append("teams")
    if players:
        targets.append("players")
    if stats:
        targets.append("stats")
    if match_id is not None:
        targets.append(f"match:{match_id}")
    if event_id is not None:
        targets.append(f"event:{event_id}")
    startup_panel(
        title="vlr-scraper · run config",
        rows={
            "Target": ", ".join(targets) or "(none — see --help)",
            "DB path": settings.DB_PATH,
            "Base URL": settings.BASE_URL,
            "Rate limit RPS": settings.RATE_LIMIT_RPS,
            "Concurrency": settings.CONCURRENCY,
            "Output format": "sqlite (export separately)",
            "Force rescrape": force_rescrape,
            "Dry run": dry_run,
        },
    )
    with timed_run() as elapsed:
        asyncio.run(
            _scrape_main(
                do_all=scrape_all,
                do_events=events,
                do_matches=matches,
                matches_only=matches_only,
                do_teams=teams,
                do_players=players,
                do_stats=stats,
                match_id=match_id,
                event_id=event_id,
                force_rescrape=force_rescrape,
                retry_failed=retry_failed,
                dry_run=dry_run,
            )
        )
    end_summary_table(
        title="Scrape summary",
        rows=[
            ("Targets", ", ".join(targets) or "—"),
            ("DB path", settings.DB_PATH),
            ("Errors log", settings.ERRORS_LOG),
        ],
        duration_s=elapsed[0],
    )


def _parse_vlr_target_url(url: str) -> tuple[str | None, int | None, str]:
    """
    Returns (target_type, target_id, normalized_url)
    target_type in {"event", "match", None}
    """
    normalized = (url or "").strip()
    if not normalized:
        return None, None, normalized

    # Accept plain IDs as convenience.
    if normalized.isdigit():
        return "match", int(normalized), f"{settings.BASE_URL}/{normalized}/"

    # Normalize missing scheme.
    if normalized.startswith("www.vlr.gg"):
        normalized = f"https://{normalized}"
    elif normalized.startswith("vlr.gg"):
        normalized = f"https://www.{normalized}"

    event_id = extract_event_id(normalized)
    if event_id:
        return "event", event_id, normalized

    match_id = extract_match_id(normalized)
    if match_id:
        return "match", match_id, normalized

    return None, None, normalized


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _make_in_clause(values: list[int]) -> tuple[str, tuple]:
    if not values:
        return "(NULL)", tuple()
    placeholders = ", ".join(["?"] * len(values))
    return f"({placeholders})", tuple(values)


async def _build_custom_scrape_payload(
    target_type: str,
    target_id: int,
    source_url: str,
) -> dict:
    if target_type == "event":
        event_rows = _rows_to_dicts(
            await execute_read("SELECT * FROM events WHERE event_id=?", (target_id,))
        )
        match_rows = _rows_to_dicts(
            await execute_read("SELECT * FROM matches WHERE event_id=?", (target_id,))
        )
    else:
        event_rows = []
        match_rows = _rows_to_dicts(
            await execute_read("SELECT * FROM matches WHERE match_id=?", (target_id,))
        )

    match_ids = sorted({int(r["match_id"]) for r in match_rows if r.get("match_id") is not None})
    match_in, match_params = _make_in_clause(match_ids)

    maps_played = _rows_to_dicts(
        await execute_read(f"SELECT * FROM maps_played WHERE match_id IN {match_in}", match_params)
    )
    match_player_stats = _rows_to_dicts(
        await execute_read(
            f"SELECT * FROM match_player_stats WHERE match_id IN {match_in}",
            match_params,
        )
    )
    match_performance = _rows_to_dicts(
        await execute_read(
            f"SELECT * FROM match_performance WHERE match_id IN {match_in}",
            match_params,
        )
    )
    kill_matrix = _rows_to_dicts(
        await execute_read(f"SELECT * FROM kill_matrix WHERE match_id IN {match_in}", match_params)
    )
    match_logs = _rows_to_dicts(
        await execute_read(f"SELECT * FROM match_logs WHERE match_id IN {match_in}", match_params)
    )
    match_economy = _rows_to_dicts(
        await execute_read(
            f"SELECT * FROM match_economy WHERE match_id IN {match_in}", match_params
        )
    )
    match_economy_rounds = _rows_to_dicts(
        await execute_read(
            f"SELECT * FROM match_economy_rounds WHERE match_id IN {match_in}",
            match_params,
        )
    )

    team_ids = set()
    event_ids = set()
    for row in match_rows:
        if row.get("team1_id") is not None:
            team_ids.add(int(row["team1_id"]))
        if row.get("team2_id") is not None:
            team_ids.add(int(row["team2_id"]))
        if row.get("event_id") is not None:
            event_ids.add(int(row["event_id"]))
    for row in match_player_stats + match_performance + match_economy + match_economy_rounds:
        if row.get("team_id") is not None:
            team_ids.add(int(row["team_id"]))
    if target_type == "event":
        event_ids.add(target_id)

    player_ids = set()
    for row in match_player_stats + match_performance:
        if row.get("player_id") is not None:
            player_ids.add(int(row["player_id"]))
    for row in kill_matrix:
        if row.get("killer_player_id") is not None:
            player_ids.add(int(row["killer_player_id"]))
        if row.get("victim_player_id") is not None:
            player_ids.add(int(row["victim_player_id"]))
    for row in match_logs:
        for key in ("killer_player_id", "victim_player_id", "spike_planted_by"):
            if row.get(key) is not None:
                player_ids.add(int(row[key]))

    team_ids_sorted = sorted(team_ids)
    player_ids_sorted = sorted(player_ids)
    event_ids_sorted = sorted(event_ids)

    team_in, team_params = _make_in_clause(team_ids_sorted)
    player_in, player_params = _make_in_clause(player_ids_sorted)
    event_in, event_params = _make_in_clause(event_ids_sorted)

    teams = _rows_to_dicts(
        await execute_read(f"SELECT * FROM teams WHERE team_id IN {team_in}", team_params)
    )
    team_rosters = _rows_to_dicts(
        await execute_read(f"SELECT * FROM team_rosters WHERE team_id IN {team_in}", team_params)
    )
    team_map_stats = _rows_to_dicts(
        await execute_read(f"SELECT * FROM team_map_stats WHERE team_id IN {team_in}", team_params)
    )
    players = _rows_to_dicts(
        await execute_read(f"SELECT * FROM players WHERE player_id IN {player_in}", player_params)
    )
    player_career_stats = _rows_to_dicts(
        await execute_read(
            f"SELECT * FROM player_career_stats WHERE player_id IN {player_in}",
            player_params,
        )
    )
    global_player_stats = _rows_to_dicts(
        await execute_read(
            f"SELECT * FROM global_player_stats WHERE player_id IN {player_in}",
            player_params,
        )
    )
    rankings = _rows_to_dicts(
        await execute_read(f"SELECT * FROM rankings WHERE team_id IN {team_in}", team_params)
    )
    events = _rows_to_dicts(
        await execute_read(f"SELECT * FROM events WHERE event_id IN {event_in}", event_params)
    )
    if target_type == "event" and event_rows:
        events = event_rows

    return {
        "meta": {
            "source_url": source_url,
            "target_type": target_type,
            "target_id": target_id,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "schema": "vlr_custom_scrape_v1",
        },
        "events": events,
        "matches": match_rows,
        "maps_played": maps_played,
        "match_player_stats": match_player_stats,
        "match_performance": match_performance,
        "kill_matrix": kill_matrix,
        "match_logs": match_logs,
        "match_economy": match_economy,
        "match_economy_rounds": match_economy_rounds,
        "teams": teams,
        "team_rosters": team_rosters,
        "team_map_stats": team_map_stats,
        "players": players,
        "player_career_stats": player_career_stats,
        "global_player_stats": global_player_stats,
        "rankings": rankings,
    }


async def _write_custom_scrape_json(
    target_type: str,
    target_id: int,
    source_url: str,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = await _build_custom_scrape_payload(
        target_type=target_type,
        target_id=target_id,
        source_url=source_url,
    )
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = output_dir / f"{target_type}_{target_id}_{ts}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


@app.command(name="scrape-link")
def scrape_link(
    url: str = typer.Option(..., "--url", help="VLR event or match URL"),
    force_rescrape: bool = typer.Option(
        False, "--force-rescrape", help="Re-scrape done queue items for relevant phases"
    ),
) -> None:
    """
    Scrape from specific VLR link:
    - Event URL -> event page -> all event matches -> teams -> players
    - Match URL -> that match -> teams -> players
    """
    _setup_logging()

    async def _run() -> None:
        signal.signal(signal.SIGINT, _handle_shutdown)
        signal.signal(signal.SIGTERM, _handle_shutdown)

        await init_db()
        await queries.queue_reset_in_progress()

        target_type, target_id, normalized = _parse_vlr_target_url(url)
        if target_type is None or target_id is None:
            console.print(
                "[red]Could not parse VLR URL. Use /event/{id}/... or /{match_id}/...[/red]"
            )
            raise typer.Exit(1)

        if target_type == "event":
            slug = ""
            m = re.search(r"/event/\d+/([^/?#]+)", normalized)
            if m:
                slug = m.group(1)

            console.print(f"[bold cyan]Target: Event {target_id}[/bold cyan]")
            async with EventsScraper() as scraper:
                await scraper.scrape_event_page(target_id, slug)

            console.print("[bold cyan]Phase A: Event matches...[/bold cyan]")
            async with MatchScraper() as scraper:
                await _process_queue(
                    "match", scraper.scrape_match_url, force_rescrape=force_rescrape
                )

        elif target_type == "match":
            console.print(f"[bold cyan]Target: Match {target_id}[/bold cyan]")
            async with MatchScraper() as scraper:
                await scraper.scrape_match_url(normalized)

        console.print("[bold cyan]Phase B: Teams...[/bold cyan]")
        async with TeamScraper() as scraper:
            await _process_queue("team", scraper.scrape_team_url, force_rescrape=force_rescrape)

        console.print("[bold cyan]Phase C: Players...[/bold cyan]")
        async with PlayerScraper() as scraper:
            await _process_queue("player", scraper.scrape_player_url, force_rescrape=force_rescrape)

        out_path = await _write_custom_scrape_json(
            target_type=target_type,
            target_id=target_id,
            source_url=normalized,
            output_dir=Path("custom_scrapes"),
        )
        console.print(f"[green]Custom scrape saved -> {out_path}[/green]")

    asyncio.run(_run())


async def _scrape_dry_run(
    do_all: bool = False,
    do_events: bool = False,
    do_matches: bool = False,
    matches_only: bool = False,
    do_teams: bool = False,
    do_players: bool = False,
    do_stats: bool = False,
    match_id: int | None = None,
    event_id: int | None = None,
) -> None:
    rows = []
    if match_id is not None:
        rows.append(("single_match", f"{settings.BASE_URL}/{match_id}/"))
    if event_id is not None:
        rows.append(("single_event", f"{settings.BASE_URL}/event/{event_id}/"))
    if matches_only:
        rows.append(("queue", "process ready match rows only"))
    if do_all or do_events:
        rows.append(("phase", "seed event regions, then process event queue"))
    if do_all or do_matches:
        rows.append(("phase", "process ready match queue"))
    if do_all or do_teams:
        rows.append(("phase", "process ready team queue"))
    if do_all or do_players:
        rows.append(("phase", "process ready player queue"))
    if do_all or do_stats:
        rows.append(("phase", "scrape global stats, rankings, per-event stats"))
    if not rows:
        rows.append(("noop", "no scrape target selected"))

    table = Table(title="Dry Run Plan", show_header=True)
    table.add_column("Kind", style="cyan")
    table.add_column("Action", style="white")
    for kind, action in rows:
        table.add_row(kind, action)
    console.print(table)


async def _scrape_main(
    do_all: bool = False,
    do_events: bool = False,
    do_matches: bool = False,
    matches_only: bool = False,
    do_teams: bool = False,
    do_players: bool = False,
    do_stats: bool = False,
    match_id: int | None = None,
    event_id: int | None = None,
    force_rescrape: bool = False,
    retry_failed: bool = False,
    dry_run: bool = False,
) -> None:
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    if dry_run:
        await _scrape_dry_run(
            do_all=do_all,
            do_events=do_events,
            do_matches=do_matches,
            matches_only=matches_only,
            do_teams=do_teams,
            do_players=do_players,
            do_stats=do_stats,
            match_id=match_id,
            event_id=event_id,
        )
        return

    await init_db()

    # Crash recovery: reset any in_progress items
    reset_count = await queries.queue_reset_in_progress()
    if reset_count:
        logger.info(f"Crash recovery: reset {reset_count} in_progress †’ pending")

    # Single match
    if match_id is not None:
        async with MatchScraper() as scraper:
            await scraper.scrape_match_id(match_id)
        return

    # Single event
    if event_id is not None:
        async with EventsScraper() as scraper:
            await scraper.scrape_event_page(event_id)
        async with MatchScraper() as scraper:
            await _process_queue(
                "match",
                scraper.scrape_match_url,
                concurrency=1,
                force_rescrape=force_rescrape,
                retry_failed=retry_failed,
            )
        return

    # Matches-only shortcut: skip seeding/events/teams/players and process queued matches only.
    if matches_only:
        console.print("[bold cyan]Matches-only: Processing queued matches...[/bold cyan]")
        async with MatchScraper() as scraper:
            await _process_queue(
                "match",
                scraper.scrape_match_url,
                concurrency=1,
                force_rescrape=force_rescrape,
                retry_failed=retry_failed,
            )
        return

    # Phase 1: Seed queue from events
    if do_all or do_events:
        console.print("[bold cyan]Phase 1: Seeding queue from events...[/bold cyan]")
        async with EventsScraper() as scraper:
            await scraper.seed_all_regions()
            console.print("[bold cyan]Phase 1b: Processing event pages...[/bold cyan]")
            await _process_queue(
                "event",
                scraper.scrape_event_url,
                force_rescrape=force_rescrape,
                retry_failed=retry_failed,
            )

    # Phase 2: Matches
    if do_all or do_matches:
        console.print("[bold cyan]Phase 2: Processing matches...[/bold cyan]")
        async with MatchScraper() as scraper:
            await _process_queue(
                "match",
                scraper.scrape_match_url,
                concurrency=1,
                force_rescrape=force_rescrape,
                retry_failed=retry_failed,
            )

    # Phase 3: Teams
    if do_all or do_teams:
        console.print("[bold cyan]Phase 3: Processing teams...[/bold cyan]")
        async with TeamScraper() as scraper:
            await _process_queue(
                "team",
                scraper.scrape_team_url,
                force_rescrape=force_rescrape,
                retry_failed=retry_failed,
            )

    # Phase 4: Players
    if do_all or do_players:
        console.print("[bold cyan]Phase 4: Processing players...[/bold cyan]")
        async with PlayerScraper() as scraper:
            await _process_queue(
                "player",
                scraper.scrape_player_url,
                force_rescrape=force_rescrape,
                retry_failed=retry_failed,
            )

    # Phase 5: Global stats + rankings
    if do_all or do_stats:
        console.print("[bold cyan]Phase 5: Global stats...[/bold cyan]")
        async with StatsScraper() as scraper:
            await scraper.scrape_all()

        console.print("[bold cyan]Phase 5b: Rankings...[/bold cyan]")
        async with RankingsScraper() as scraper:
            await scraper.scrape_all()

        # Per-event stats for all known events
        console.print("[bold cyan]Phase 5c: Per-event stats...[/bold cyan]")
        event_ids = await queries.get_all_event_ids()
        async with StatsScraper() as scraper:
            for eid in event_ids:
                if _shutdown_event.is_set():
                    break
                await scraper.scrape_for_event(eid)


# -----------------------------------------------------------------------
# update command
# -----------------------------------------------------------------------


@app.command()
def update(
    new_only: bool = typer.Option(False, "--new-only", help="Only process new queue items"),
    since: int | None = typer.Option(
        None, "--since", help="Rescrape event/match queue items updated in last N days"
    ),
) -> None:
    """Update the database with new matches and events."""
    _setup_logging()

    async def _run():
        await init_db()
        await queries.queue_reset_in_progress()
        if since is not None:
            if since < 0:
                raise typer.BadParameter("--since must be >= 0")
            now_dt = datetime.now(UTC)
            cutoff_dt = now_dt.timestamp() - (since * 86400)
            now_iso = now_dt.isoformat()
            cutoff_iso = datetime.fromtimestamp(cutoff_dt, tz=UTC).isoformat()
            await execute_write(
                """
                UPDATE crawl_queue
                SET status='pending', retries=0, last_error=NULL, next_attempt_at=NULL, updated_at=?
                WHERE page_type IN ('event', 'match') AND updated_at >= ?
                """,
                (now_iso, cutoff_iso),
            )
        elif not new_only:
            await queries.queue_retry_failed_for_page_type("event")
            await queries.queue_retry_failed_for_page_type("match")

        console.print("[bold cyan]Seeding queue for updates...[/bold cyan]")
        async with EventsScraper() as scraper:
            await scraper.seed_all_regions()
            console.print("[bold cyan]Processing event pages for updates...[/bold cyan]")
            await _process_queue("event", scraper.scrape_event_url)

        async with MatchScraper() as scraper:
            await _process_queue("match", scraper.scrape_match_url)

    asyncio.run(_run())


# -----------------------------------------------------------------------
# export command
# -----------------------------------------------------------------------


@app.command()
def export(
    table: str | None = typer.Option(None, "--table", help="Table name to export"),
    all_tables: bool = typer.Option(False, "--all", help="Export all tables"),
    fmt: str = typer.Option("json", "--format", help="Output format: json|csv|parquet"),
    output: str = typer.Option("./exports/", "--output", help="Output directory"),
) -> None:
    """Export database tables to JSON, CSV, or Parquet."""
    _setup_logging()
    output_dir = Path(output)

    if fmt not in ("json", "csv", "parquet"):
        console.print(f"[red]Unknown format: {fmt}. Use json, csv, or parquet.[/red]")
        raise typer.Exit(1)

    startup_panel(
        title="vlr-scraper · export",
        rows={
            "DB path": settings.DB_PATH,
            "Output format": fmt,
            "Export dir": output_dir,
            "Scope": table or ("all tables" if all_tables else "(none)"),
        },
    )

    async def _run() -> list[str]:
        written: list[str] = []
        if all_tables:
            await export_all(fmt=fmt, output_dir=output_dir)
            written = [str(output_dir / f"{t}.{fmt}") for t in EXPORTABLE_TABLES]
        elif table:
            if table not in EXPORTABLE_TABLES:
                console.print(f"[red]Unknown table: {table}[/red]")
                console.print(f"Available: {', '.join(EXPORTABLE_TABLES)}")
                raise typer.Exit(1)
            path = await export_table(table=table, fmt=fmt, output_dir=output_dir)
            written.append(str(path))
        else:
            console.print("[red]Specify --table or --all[/red]")
            raise typer.Exit(1)
        return written

    with timed_run() as elapsed:
        paths = asyncio.run(_run())
    end_summary_table(
        title="Export summary",
        rows=[("Format", fmt), ("Files", len(paths))],
        outputs=paths,
        duration_s=elapsed[0],
    )

# -----------------------------------------------------------------------
# status command
# -----------------------------------------------------------------------


@app.command()
def status() -> None:
    """Show crawl queue progress counts."""
    _setup_logging()

    async def _run():
        await init_db()
        counts = await queries.queue_dashboard_counts()
        table = Table(title="Crawl Queue Status", show_header=True)
        table.add_column("Bucket", style="cyan")
        table.add_column("Count", style="magenta", justify="right")

        total = sum(counts.values())
        for status_name in ("pending_ready", "pending_cooldown", "in_progress", "done", "failed"):
            count = counts.get(status_name, 0)
            table.add_row(status_name, str(count))
        table.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]")

        console.print(table)

        done = counts.get("done", 0)
        pending = counts.get("pending_ready", 0) + counts.get("pending_cooldown", 0)
        if total > 0:
            pct = done / total * 100
            console.print(f"\n[green]Progress: {pct:.1f}% complete ({done}/{total})[/green]")
            if pending:
                console.print(f"[yellow]{pending} items remaining[/yellow]")

        pending_rows = await queries.queue_oldest_pending(limit=5)
        if pending_rows:
            pending_table = Table(title="Oldest Pending", show_header=True)
            for col in ("queue_id", "page_type", "retries", "next_attempt_at", "url"):
                pending_table.add_column(col)
            for row in pending_rows:
                pending_table.add_row(
                    str(row["queue_id"]),
                    str(row["page_type"]),
                    str(row["retries"]),
                    str(row["next_attempt_at"] or ""),
                    str(row["url"]),
                )
            console.print(pending_table)

        errors = await queries.queue_error_summary(limit=5)
        if errors:
            err_table = Table(title="Top Queue Errors", show_header=True)
            err_table.add_column("Count", justify="right")
            err_table.add_column("Error")
            for row in errors:
                err_table.add_row(str(row["cnt"]), str(row["last_error"])[:120])
            console.print(err_table)

        runs = await queries.scrape_runs_recent(limit=5)
        if runs:
            run_table = Table(title="Recent Runs", show_header=True)
            for col in (
                "run_id",
                "mode",
                "status",
                "pages_processed",
                "pages_failed",
                "cloudflare_blocks",
                "cloakbrowser_successes",
            ):
                run_table.add_column(col)
            for row in runs:
                run_table.add_row(
                    str(row["run_id"]),
                    str(row["mode"]),
                    str(row["status"]),
                    str(row["pages_processed"]),
                    str(row["pages_failed"]),
                    str(row["cloudflare_blocks"]),
                    str(row["cloakbrowser_successes"]),
                )
            console.print(run_table)

    asyncio.run(_run())


@app.command(name="queue")
def queue_dashboard() -> None:
    """Show detailed crawl queue dashboard."""
    status()


# -----------------------------------------------------------------------
# stats (DB row counts) command
# -----------------------------------------------------------------------


@app.command(name="stats")
def db_stats() -> None:
    """Show row counts for all database tables."""
    _setup_logging()

    async def _run():
        counts = await queries.get_table_counts()
        status_table("Database row counts", counts)

    asyncio.run(_run())


# -----------------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------------


def _setup_logging() -> None:
    configure_rich_logging(settings.LOG_LEVEL, Path(settings.ERRORS_LOG))


def _interactive_menu() -> None:
    """Simple interactive menu when script runs without command args."""
    console.print("\n[bold cyan]VLR Scraper Menu[/bold cyan]")
    console.print("1) Scrape all")
    console.print("2) Scrape events")
    console.print("3) Scrape matches")
    console.print("4) Scrape teams")
    console.print("5) Scrape players")
    console.print("6) Scrape stats + rankings")
    console.print("7) Scrape from VLR link")
    console.print("8) Export all JSON")
    console.print("9) Status")
    console.print("10) DB table stats")

    choice = typer.prompt("Choose option", default="1").strip()

    if choice == "1":
        scrape(scrape_all=True)
    elif choice == "2":
        scrape(events=True)
    elif choice == "3":
        scrape(matches=True)
    elif choice == "4":
        scrape(teams=True)
    elif choice == "5":
        scrape(players=True)
    elif choice == "6":
        scrape(stats=True)
    elif choice == "7":
        url = typer.prompt("Paste VLR event/match URL").strip()
        force = typer.confirm("Force re-scrape done queue items?", default=False)
        scrape_link(url=url, force_rescrape=force)
    elif choice == "8":
        export(all_tables=True, fmt="json", output="./exports/")
    elif choice == "9":
        status()
    elif choice == "10":
        db_stats()
    else:
        console.print("[red]Invalid choice.[/red]")


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) == 1:
        _interactive_menu()
    else:
        app()
