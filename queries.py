"""Async upsert and query functions for every database table."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from config import settings
from connection import (
    execute_read,
    execute_read_one,
    execute_write,
    execute_write_many,
    execute_write_returning,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _after_minutes(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# crawl_queue
# ---------------------------------------------------------------------------


async def queue_add(url: str, page_type: str) -> None:
    await execute_write(
        """
        INSERT OR IGNORE INTO crawl_queue (url, page_type, status, retries, created_at, updated_at)
        VALUES (?, ?, 'pending', 0, ?, ?)
        """,
        (url, page_type, _now(), _now()),
    )


async def queue_add_many(rows: list[tuple[str, str]]) -> None:
    """rows = [(url, page_type), ...]"""
    now = _now()
    params = [(url, pt, "pending", 0, now, now) for url, pt in rows]
    await execute_write_many(
        """
        INSERT OR IGNORE INTO crawl_queue (url, page_type, status, retries, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        params,
    )


async def queue_next_pending(page_type: str | None = None, limit: int = 50) -> list[Any]:
    if page_type:
        return await execute_read(
            """
            SELECT * FROM crawl_queue
            WHERE status='pending'
              AND page_type=?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY updated_at ASC, queue_id ASC
            LIMIT ?
            """,
            (page_type, _now(), limit),
        )
    return await execute_read(
        """
        SELECT * FROM crawl_queue
        WHERE status='pending'
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ORDER BY updated_at ASC, queue_id ASC
        LIMIT ?
        """,
        (_now(), limit),
    )


async def queue_claim_pending(page_type: str | None = None, limit: int = 50) -> list[Any]:
    now = _now()
    if page_type:
        return await execute_write_returning(
            """
            WITH picked AS (
                SELECT queue_id FROM crawl_queue
                WHERE status='pending'
                  AND page_type=?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY updated_at ASC, queue_id ASC
                LIMIT ?
            )
            UPDATE crawl_queue
            SET status='in_progress', updated_at=?
            WHERE queue_id IN (SELECT queue_id FROM picked)
            RETURNING *
            """,
            (page_type, now, limit, now),
        )
    return await execute_write_returning(
        """
        WITH picked AS (
            SELECT queue_id FROM crawl_queue
            WHERE status='pending'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY updated_at ASC, queue_id ASC
            LIMIT ?
        )
        UPDATE crawl_queue
        SET status='in_progress', updated_at=?
        WHERE queue_id IN (SELECT queue_id FROM picked)
        RETURNING *
        """,
        (now, limit, now),
    )


async def queue_mark_in_progress(queue_id: int) -> None:
    await execute_write(
        "UPDATE crawl_queue SET status='in_progress', updated_at=? WHERE queue_id=?",
        (_now(), queue_id),
    )


async def queue_mark_done(queue_id: int) -> None:
    await execute_write(
        "UPDATE crawl_queue SET status='done', updated_at=? WHERE queue_id=?",
        (_now(), queue_id),
    )


async def queue_mark_failed(queue_id: int, error: str) -> None:
    await execute_write(
        """
        UPDATE crawl_queue
        SET status='failed', last_error=?, retries=retries+1, updated_at=?
        WHERE queue_id=?
        """,
        (error[:2000], _now(), queue_id),
    )


async def queue_retry_failed(
    queue_id: int,
    error: str = "Retry requested",
    cooldown_minutes: int | None = None,
) -> None:
    if cooldown_minutes is None:
        row = await execute_read_one(
            "SELECT retries FROM crawl_queue WHERE queue_id=?", (queue_id,)
        )
        next_retry = int(row["retries"] or 0) + 1 if row else 1
        cooldown = min(
            settings.CLOUDFLARE_COOLDOWN_MINUTES * (2 ** max(0, next_retry - 1)),
            settings.CLOUDFLARE_COOLDOWN_MAX_MINUTES,
        )
    else:
        cooldown = cooldown_minutes
    next_attempt_at = _after_minutes(cooldown) if cooldown > 0 else None
    await execute_write(
        """
        UPDATE crawl_queue
        SET status='pending', last_error=?, retries=retries+1, next_attempt_at=?, updated_at=?
        WHERE queue_id=?
        """,
        (error[:2000], next_attempt_at, _now(), queue_id),
    )


async def queue_reset_in_progress() -> int:
    """On startup: reset any in_progress rows to pending (crash recovery)."""
    rows = await execute_read("SELECT queue_id FROM crawl_queue WHERE status='in_progress'")
    if rows:
        ids = [r["queue_id"] for r in rows]
        await execute_write_many(
            "UPDATE crawl_queue SET status='pending', next_attempt_at=NULL, updated_at=? WHERE queue_id=?",
            [(_now(), qid) for qid in ids],
        )
    return len(rows)


async def queue_counts() -> dict[str, int]:
    rows = await execute_read("SELECT status, COUNT(*) as cnt FROM crawl_queue GROUP BY status")
    return {r["status"]: r["cnt"] for r in rows}


async def queue_dashboard_counts() -> dict[str, int]:
    now = _now()
    rows = await execute_read(
        """
        SELECT
            SUM(CASE WHEN status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) THEN 1 ELSE 0 END) AS pending_ready,
            SUM(CASE WHEN status='pending' AND next_attempt_at > ? THEN 1 ELSE 0 END) AS pending_cooldown,
            SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
        FROM crawl_queue
        """,
        (now, now),
    )
    row = rows[0] if rows else {}
    return {key: int(row[key] or 0) for key in row.keys()}


async def queue_error_summary(limit: int = 10) -> list[Any]:
    return await execute_read(
        """
        SELECT last_error, COUNT(*) AS cnt
        FROM crawl_queue
        WHERE last_error IS NOT NULL AND last_error != ''
        GROUP BY last_error
        ORDER BY cnt DESC
        LIMIT ?
        """,
        (limit,),
    )


async def queue_oldest_pending(limit: int = 10) -> list[Any]:
    return await execute_read(
        """
        SELECT queue_id, page_type, url, retries, next_attempt_at, updated_at
        FROM crawl_queue
        WHERE status='pending'
        ORDER BY updated_at ASC, queue_id ASC
        LIMIT ?
        """,
        (limit,),
    )


async def scrape_run_start(mode: str) -> int:
    return await execute_write(
        """
        INSERT INTO scrape_runs (mode, started_at, status)
        VALUES (?, ?, 'running')
        """,
        (mode, _now()),
    )


async def scrape_run_finish(
    run_id: int,
    status: str,
    pages_processed: int,
    pages_failed: int,
    cloudflare_blocks: int,
    cloakbrowser_successes: int,
    last_error: str | None = None,
) -> None:
    await execute_write(
        """
        UPDATE scrape_runs
        SET finished_at=?, status=?, pages_processed=?, pages_failed=?,
            cloudflare_blocks=?, cloakbrowser_successes=?, last_error=?
        WHERE run_id=?
        """,
        (
            _now(),
            status,
            pages_processed,
            pages_failed,
            cloudflare_blocks,
            cloakbrowser_successes,
            last_error[:2000] if last_error else None,
            run_id,
        ),
    )


async def scrape_runs_recent(limit: int = 10) -> list[Any]:
    return await execute_read(
        """
        SELECT * FROM scrape_runs
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    )


async def queue_reset_all_for_rescrape() -> None:
    await execute_write(
        "UPDATE crawl_queue SET status='pending', retries=0, last_error=NULL, next_attempt_at=NULL, updated_at=?",
        (_now(),),
    )


async def queue_reset_for_page_type(page_type: str) -> None:
    """Reset only one page_type to pending for targeted re-scrape."""
    await execute_write(
        """
        UPDATE crawl_queue
        SET status='pending', retries=0, last_error=NULL, next_attempt_at=NULL, updated_at=?
        WHERE page_type=?
        """,
        (_now(), page_type),
    )


async def queue_retry_failed_for_page_type(page_type: str) -> int:
    """Reset only failed entries for a page_type back to pending."""
    rows = await execute_read(
        "SELECT queue_id FROM crawl_queue WHERE page_type=? AND status='failed'",
        (page_type,),
    )
    if rows:
        ids = [r["queue_id"] for r in rows]
        await execute_write_many(
            "UPDATE crawl_queue SET status='pending', retries=0, last_error=NULL, next_attempt_at=NULL, updated_at=? WHERE queue_id=?",
            [(_now(), qid) for qid in ids],
        )
    return len(rows)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def upsert_event(data: dict[str, Any]) -> None:
    await execute_write(
        """
        INSERT INTO events
            (event_id, name, slug, status, region, tier, prize_pool,
             start_date, end_date, logo_url, url, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            name=excluded.name,
            slug=excluded.slug,
            status=excluded.status,
            region=excluded.region,
            tier=excluded.tier,
            prize_pool=excluded.prize_pool,
            start_date=excluded.start_date,
            end_date=excluded.end_date,
            logo_url=excluded.logo_url,
            url=excluded.url,
            scraped_at=excluded.scraped_at
        """,
        (
            data["event_id"],
            data["name"],
            data.get("slug"),
            data.get("status"),
            data.get("region"),
            data.get("tier"),
            data.get("prize_pool"),
            data.get("start_date"),
            data.get("end_date"),
            data.get("logo_url"),
            data.get("url"),
            data.get("scraped_at", _now()),
        ),
    )


async def ensure_event(event_id: int, url: str | None = None) -> None:
    """Insert minimal event row only if missing (preserve existing richer row)."""
    await execute_write(
        """
        INSERT OR IGNORE INTO events (event_id, name, url, scraped_at)
        VALUES (?, ?, ?, ?)
        """,
        (event_id, f"Event {event_id}", url, _now()),
    )


async def get_all_event_ids() -> list[int]:
    rows = await execute_read("SELECT event_id FROM events")
    return [r["event_id"] for r in rows]


# ---------------------------------------------------------------------------
# teams
# ---------------------------------------------------------------------------


async def upsert_team(data: dict[str, Any]) -> None:
    await execute_write(
        """
        INSERT INTO teams
            (team_id, name, abbreviation, logo_url, country, region,
             is_active, url, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            name=excluded.name,
            abbreviation=excluded.abbreviation,
            logo_url=excluded.logo_url,
            country=excluded.country,
            region=excluded.region,
            is_active=excluded.is_active,
            url=excluded.url,
            scraped_at=excluded.scraped_at
        """,
        (
            data["team_id"],
            data["name"],
            data.get("abbreviation"),
            data.get("logo_url"),
            data.get("country"),
            data.get("region"),
            data.get("is_active", 1),
            data.get("url"),
            data.get("scraped_at", _now()),
        ),
    )


async def ensure_team(team_id: int, url: str | None = None) -> None:
    """Insert minimal team row only if missing (preserve existing richer row)."""
    await execute_write(
        """
        INSERT OR IGNORE INTO teams
            (team_id, name, is_active, url, scraped_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (team_id, f"Team {team_id}", 1, url, _now()),
    )


async def ensure_teams(team_ids: list[int]) -> None:
    unique_ids = sorted(set(team_ids))
    params = [(team_id, f"Team {team_id}", 1, _now()) for team_id in unique_ids]
    await execute_write_many(
        """
        INSERT OR IGNORE INTO teams
            (team_id, name, is_active, scraped_at)
        VALUES (?, ?, ?, ?)
        """,
        params,
    )


# ---------------------------------------------------------------------------
# players
# ---------------------------------------------------------------------------


async def upsert_player(data: dict[str, Any]) -> None:
    await execute_write(
        """
        INSERT INTO players
            (player_id, ign, real_name, country, country_flag,
             current_team_id, role, twitter, twitch, url, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET
            ign=excluded.ign,
            real_name=excluded.real_name,
            country=excluded.country,
            country_flag=excluded.country_flag,
            current_team_id=excluded.current_team_id,
            role=excluded.role,
            twitter=excluded.twitter,
            twitch=excluded.twitch,
            url=excluded.url,
            scraped_at=excluded.scraped_at
        """,
        (
            data["player_id"],
            data["ign"],
            data.get("real_name"),
            data.get("country"),
            data.get("country_flag"),
            data.get("current_team_id"),
            data.get("role"),
            data.get("twitter"),
            data.get("twitch"),
            data.get("url"),
            data.get("scraped_at", _now()),
        ),
    )


async def ensure_player(player_id: int, url: str | None = None) -> None:
    """Insert minimal player row only if missing (preserve existing richer row)."""
    await execute_write(
        """
        INSERT OR IGNORE INTO players (player_id, ign, url, scraped_at)
        VALUES (?, ?, ?, ?)
        """,
        (player_id, f"Player {player_id}", url, _now()),
    )


async def ensure_players(player_ids: list[int]) -> None:
    unique_ids = sorted(set(player_ids))
    params = [(player_id, f"Player {player_id}", _now()) for player_id in unique_ids]
    await execute_write_many(
        """
        INSERT OR IGNORE INTO players (player_id, ign, scraped_at)
        VALUES (?, ?, ?)
        """,
        params,
    )


# ---------------------------------------------------------------------------
# team_rosters
# ---------------------------------------------------------------------------


async def upsert_roster_entry(data: dict[str, Any]) -> None:
    # Check for existing entry first
    existing = await execute_read_one(
        "SELECT roster_id FROM team_rosters WHERE team_id=? AND player_id=? AND is_current=?",
        (data["team_id"], data["player_id"], data.get("is_current", 1)),
    )
    if existing:
        return
    await execute_write(
        """
        INSERT INTO team_rosters (team_id, player_id, join_date, leave_date, is_current)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            data["team_id"],
            data["player_id"],
            data.get("join_date"),
            data.get("leave_date"),
            data.get("is_current", 1),
        ),
    )


async def upsert_roster_entries(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        await upsert_roster_entry(entry)


# ---------------------------------------------------------------------------
# matches
# ---------------------------------------------------------------------------


async def upsert_match(data: dict[str, Any]) -> None:
    await execute_write(
        """
        INSERT INTO matches
            (match_id, event_id, stage_name, series_name, team1_id, team2_id,
             team1_score, team2_score, winner_team_id, status, scheduled_at,
             unix_timestamp, best_of, vod_url, url, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            event_id=excluded.event_id,
            stage_name=excluded.stage_name,
            series_name=excluded.series_name,
            team1_id=excluded.team1_id,
            team2_id=excluded.team2_id,
            team1_score=excluded.team1_score,
            team2_score=excluded.team2_score,
            winner_team_id=excluded.winner_team_id,
            status=excluded.status,
            scheduled_at=excluded.scheduled_at,
            unix_timestamp=excluded.unix_timestamp,
            best_of=excluded.best_of,
            vod_url=excluded.vod_url,
            url=excluded.url,
            scraped_at=excluded.scraped_at
        """,
        (
            data["match_id"],
            data.get("event_id"),
            data.get("stage_name"),
            data.get("series_name"),
            data.get("team1_id"),
            data.get("team2_id"),
            data.get("team1_score"),
            data.get("team2_score"),
            data.get("winner_team_id"),
            data.get("status"),
            data.get("scheduled_at"),
            data.get("unix_timestamp"),
            data.get("best_of"),
            data.get("vod_url"),
            data.get("url"),
            data.get("scraped_at", _now()),
        ),
    )


async def ensure_match_stub(match_id: int, url: str | None = None) -> None:
    """
    Insert minimal match row only if missing.
    Useful so child tables can safely reference match_id even when overview fails.
    """
    await execute_write(
        """
        INSERT OR IGNORE INTO matches (match_id, status, url, scraped_at)
        VALUES (?, ?, ?, ?)
        """,
        (match_id, "unknown", url, _now()),
    )


# ---------------------------------------------------------------------------
# maps_played
# ---------------------------------------------------------------------------


async def insert_map_played(data: dict[str, Any]) -> int:
    return await execute_write(
        """
        INSERT INTO maps_played
            (match_id, map_number, map_name, team1_rounds, team2_rounds,
             team1_ct_rounds, team1_t_rounds, team2_ct_rounds, team2_t_rounds,
             winner_team_id, is_draw, map_duration, team1_atk_first)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["match_id"],
            data.get("map_number"),
            data.get("map_name"),
            data.get("team1_rounds"),
            data.get("team2_rounds"),
            data.get("team1_ct_rounds"),
            data.get("team1_t_rounds"),
            data.get("team2_ct_rounds"),
            data.get("team2_t_rounds"),
            data.get("winner_team_id"),
            data.get("is_draw", 0),
            data.get("map_duration"),
            data.get("team1_atk_first"),
        ),
    )


async def delete_maps_for_match(match_id: int) -> None:
    await execute_write("DELETE FROM maps_played WHERE match_id=?", (match_id,))


# ---------------------------------------------------------------------------
# match_player_stats
# ---------------------------------------------------------------------------


async def insert_player_stats_batch(rows: list[dict[str, Any]]) -> None:
    params = [
        (
            r["match_id"],
            r.get("map_play_id"),
            r["player_id"],
            r.get("team_id"),
            r.get("agent"),
            r.get("rating"),
            r.get("acs"),
            r.get("kills"),
            r.get("deaths"),
            r.get("assists"),
            r.get("kd_diff"),
            r.get("kast"),
            r.get("adr"),
            r.get("hs_pct"),
            r.get("fk"),
            r.get("fd"),
            r.get("fk_diff"),
            r.get("rounds_played"),
        )
        for r in rows
    ]
    await execute_write_many(
        """
        INSERT INTO match_player_stats
            (match_id, map_play_id, player_id, team_id, agent, rating, acs,
             kills, deaths, assists, kd_diff, kast, adr, hs_pct,
             fk, fd, fk_diff, rounds_played)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )


async def delete_player_stats_for_match(match_id: int) -> None:
    await execute_write("DELETE FROM match_player_stats WHERE match_id=?", (match_id,))


# ---------------------------------------------------------------------------
# match_performance
# ---------------------------------------------------------------------------


async def insert_performance_batch(rows: list[dict[str, Any]]) -> None:
    params = [
        (
            r["match_id"],
            r.get("map_play_id"),
            r["player_id"],
            r.get("team_id"),
            r.get("kills_2k"),
            r.get("kills_3k"),
            r.get("kills_4k"),
            r.get("kills_5k"),
            json.dumps(r.get("kills_2k_rounds") or []),
            json.dumps(r.get("kills_3k_rounds") or []),
            json.dumps(r.get("kills_4k_rounds") or []),
            json.dumps(r.get("kills_5k_rounds") or []),
            r.get("clutches_v1"),
            r.get("clutches_v2"),
            r.get("clutches_v3"),
            r.get("clutches_v4"),
            r.get("clutches_v5"),
            json.dumps(r.get("clutches_v1_rounds") or []),
            json.dumps(r.get("clutches_v2_rounds") or []),
            json.dumps(r.get("clutches_v3_rounds") or []),
            json.dumps(r.get("clutches_v4_rounds") or []),
            json.dumps(r.get("clutches_v5_rounds") or []),
        )
        for r in rows
    ]
    await execute_write_many(
        """
        INSERT INTO match_performance
            (match_id, map_play_id, player_id, team_id,
             kills_2k, kills_3k, kills_4k, kills_5k,
             kills_2k_rounds, kills_3k_rounds, kills_4k_rounds, kills_5k_rounds,
             clutches_v1, clutches_v2, clutches_v3, clutches_v4, clutches_v5,
             clutches_v1_rounds, clutches_v2_rounds, clutches_v3_rounds,
             clutches_v4_rounds, clutches_v5_rounds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )


async def delete_performance_for_match(match_id: int) -> None:
    await execute_write("DELETE FROM match_performance WHERE match_id=?", (match_id,))


# ---------------------------------------------------------------------------
# kill_matrix
# ---------------------------------------------------------------------------


async def insert_kill_matrix_batch(rows: list[dict[str, Any]]) -> None:
    params = [
        (
            r["match_id"],
            r.get("map_play_id"),
            r["killer_player_id"],
            r["victim_player_id"],
            r["kill_count"],
        )
        for r in rows
    ]
    await execute_write_many(
        """
        INSERT INTO kill_matrix
            (match_id, map_play_id, killer_player_id, victim_player_id, kill_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        params,
    )


async def delete_kill_matrix_for_match(match_id: int) -> None:
    await execute_write("DELETE FROM kill_matrix WHERE match_id=?", (match_id,))


# ---------------------------------------------------------------------------
# match_economy
# ---------------------------------------------------------------------------


async def insert_economy_batch(rows: list[dict[str, Any]]) -> None:
    params = [
        (
            r["match_id"],
            r.get("map_play_id"),
            r["team_id"],
            r.get("eco_rounds_played"),
            r.get("eco_rounds_won"),
            r.get("semi_eco_rounds_played"),
            r.get("semi_eco_rounds_won"),
            r.get("semi_buy_rounds_played"),
            r.get("semi_buy_rounds_won"),
            r.get("full_buy_rounds_played"),
            r.get("full_buy_rounds_won"),
        )
        for r in rows
    ]
    await execute_write_many(
        """
        INSERT INTO match_economy
            (match_id, map_play_id, team_id,
             eco_rounds_played, eco_rounds_won,
             semi_eco_rounds_played, semi_eco_rounds_won,
             semi_buy_rounds_played, semi_buy_rounds_won,
             full_buy_rounds_played, full_buy_rounds_won)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )


async def delete_economy_for_match(match_id: int) -> None:
    await execute_write("DELETE FROM match_economy WHERE match_id=?", (match_id,))


# ---------------------------------------------------------------------------
# match_economy_rounds
# ---------------------------------------------------------------------------


async def insert_economy_rounds_batch(rows: list[dict[str, Any]]) -> None:
    params = [
        (
            r["match_id"],
            r.get("map_play_id"),
            r["team_id"],
            r["round_number"],
            r.get("side"),
            r.get("buy_type"),
            r.get("remaining_bank"),
            r.get("loadout_value"),
            r.get("round_won"),
        )
        for r in rows
    ]
    await execute_write_many(
        """
        INSERT INTO match_economy_rounds
            (match_id, map_play_id, team_id, round_number, side, buy_type,
             remaining_bank, loadout_value, round_won)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )


async def delete_economy_rounds_for_match(match_id: int) -> None:
    await execute_write("DELETE FROM match_economy_rounds WHERE match_id=?", (match_id,))


# ---------------------------------------------------------------------------
# match_logs
# ---------------------------------------------------------------------------


async def insert_logs_batch(rows: list[dict[str, Any]]) -> None:
    params = [
        (
            r["match_id"],
            r.get("map_play_id"),
            r["round_number"],
            r["event_order"],
            r["event_type"],
            r.get("killer_player_id"),
            r.get("victim_player_id"),
            r.get("weapon"),
            r.get("is_headshot"),
            r.get("is_wallbang"),
            r.get("spike_planted_by"),
            r.get("round_winner_team"),
            r.get("round_end_reason"),
        )
        for r in rows
    ]
    await execute_write_many(
        """
        INSERT INTO match_logs
            (match_id, map_play_id, round_number, event_order, event_type,
             killer_player_id, victim_player_id, weapon, is_headshot, is_wallbang,
             spike_planted_by, round_winner_team, round_end_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )


async def delete_logs_for_match(match_id: int) -> None:
    await execute_write("DELETE FROM match_logs WHERE match_id=?", (match_id,))


# ---------------------------------------------------------------------------
# player_career_stats
# ---------------------------------------------------------------------------


async def upsert_player_career_stats(data: dict[str, Any]) -> None:
    await execute_write(
        """
        DELETE FROM player_career_stats
        WHERE player_id=? AND event_id IS ? AND agent IS ?
        """,
        (data["player_id"], data.get("event_id"), data.get("agent")),
    )
    await execute_write(
        """
        INSERT INTO player_career_stats
            (player_id, event_id, agent, maps_played, rounds_played,
             rating, acs, kd_ratio, kast, adr, kpr, apr, fkpr, fdpr,
             hs_pct, cl_pct, cl_won, cl_played, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["player_id"],
            data.get("event_id"),
            data.get("agent"),
            data.get("maps_played"),
            data.get("rounds_played"),
            data.get("rating"),
            data.get("acs"),
            data.get("kd_ratio"),
            data.get("kast"),
            data.get("adr"),
            data.get("kpr"),
            data.get("apr"),
            data.get("fkpr"),
            data.get("fdpr"),
            data.get("hs_pct"),
            data.get("cl_pct"),
            data.get("cl_won"),
            data.get("cl_played"),
            data.get("scraped_at", _now()),
        ),
    )


# ---------------------------------------------------------------------------
# team_map_stats
# ---------------------------------------------------------------------------


async def upsert_team_map_stats(data: dict[str, Any]) -> None:
    await execute_write(
        """
        DELETE FROM team_map_stats
        WHERE team_id=? AND map_name IS ?
        """,
        (data["team_id"], data.get("map_name")),
    )
    await execute_write(
        """
        INSERT INTO team_map_stats
            (team_id, map_name, maps_played, maps_won, maps_lost, win_pct,
             atk_rounds_played, atk_rounds_won, def_rounds_played, def_rounds_won,
             scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["team_id"],
            data.get("map_name"),
            data.get("maps_played"),
            data.get("maps_won"),
            data.get("maps_lost"),
            data.get("win_pct"),
            data.get("atk_rounds_played"),
            data.get("atk_rounds_won"),
            data.get("def_rounds_played"),
            data.get("def_rounds_won"),
            data.get("scraped_at", _now()),
        ),
    )


# ---------------------------------------------------------------------------
# global_player_stats
# ---------------------------------------------------------------------------


async def upsert_global_player_stats(data: dict[str, Any]) -> None:
    agents_json = (
        json.dumps(data["agents_played"])
        if isinstance(data.get("agents_played"), list)
        else data.get("agents_played")
    )
    await execute_write(
        """
        DELETE FROM global_player_stats
        WHERE player_id=? AND region IS ? AND timespan IS ? AND event_id IS ?
        """,
        (
            data["player_id"],
            data.get("region"),
            data.get("timespan"),
            data.get("event_id"),
        ),
    )
    await execute_write(
        """
        INSERT INTO global_player_stats
            (player_id, team_id, region, timespan, event_id, agents_played,
             rating, acs, kd_ratio, kast, adr, kpr, apr, fkpr, fdpr,
             hs_pct, cl_pct, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["player_id"],
            data.get("team_id"),
            data.get("region"),
            data.get("timespan"),
            data.get("event_id"),
            agents_json,
            data.get("rating"),
            data.get("acs"),
            data.get("kd_ratio"),
            data.get("kast"),
            data.get("adr"),
            data.get("kpr"),
            data.get("apr"),
            data.get("fkpr"),
            data.get("fdpr"),
            data.get("hs_pct"),
            data.get("cl_pct"),
            data.get("scraped_at", _now()),
        ),
    )


# ---------------------------------------------------------------------------
# rankings
# ---------------------------------------------------------------------------


async def upsert_ranking(data: dict[str, Any]) -> None:
    await execute_write(
        """
        DELETE FROM rankings
        WHERE team_id=? AND region IS ?
        """,
        (data["team_id"], data.get("region")),
    )
    await execute_write(
        """
        INSERT INTO rankings (team_id, region, rank, record, earnings, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            data["team_id"],
            data.get("region"),
            data.get("rank"),
            data.get("record"),
            data.get("earnings"),
            data.get("scraped_at", _now()),
        ),
    )


# ---------------------------------------------------------------------------
# read helpers
# ---------------------------------------------------------------------------


async def get_table_counts() -> dict[str, int]:
    tables = [
        "events",
        "teams",
        "players",
        "team_rosters",
        "matches",
        "maps_played",
        "match_player_stats",
        "match_performance",
        "kill_matrix",
        "match_economy",
        "match_economy_rounds",
        "match_logs",
        "player_career_stats",
        "team_map_stats",
        "global_player_stats",
        "rankings",
    ]
    counts: dict[str, int] = {}
    for table in tables:
        row = await execute_read_one(f"SELECT COUNT(*) as cnt FROM {table}")
        counts[table] = row["cnt"] if row else 0
    return counts
