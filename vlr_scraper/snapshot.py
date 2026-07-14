"""Fleet match-level snapshot export for vlr-scraper."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

REPO_SLUG = "vlr"
GAME = "Valorant"
SCHEMA_VERSION = "1.0"
BASE_URL = "https://www.vlr.gg"

COLUMNS = [
    "match_id",
    "match_date",
    "team_a",
    "team_b",
    "winner",
    "source_url",
    "status",
    "score_a",
    "score_b",
    "event_name",
    "format",
    "raw_status",
]

STATUS_MAP = {
    "completed": "completed",
    "complete": "completed",
    "finished": "completed",
    "live": "live",
    "ongoing": "live",
    "upcoming": "scheduled",
    "scheduled": "scheduled",
    "tbd": "scheduled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "postponed": "postponed",
}

_stats = {
    "status_mapped": 0,
    "status_heuristic": 0,
    "date_status_anomaly": 0,
    "dropped_no_teams": 0,
    "rows_out": 0,
    "url_constructed": 0,
}


def _reset_stats() -> None:
    for k in _stats:
        _stats[k] = 0


def _normalize_status(raw: str | None, score_a: int | None, score_b: int | None) -> str:
    key = (raw or "").strip().lower()
    if key in STATUS_MAP:
        _stats["status_mapped"] += 1
        return STATUS_MAP[key]
    _stats["status_heuristic"] += 1
    if score_a is not None or score_b is not None:
        return "completed"
    return "scheduled"


def _parse_date(
    scheduled_at: str | None,
    unix_ts: int | float | None,
    *,
    status: str,
    has_scores: bool,
) -> str | None:
    """VLR: scheduled_at string; unix_timestamp is Unix seconds."""
    next_year = datetime.now(UTC).year + 1
    parsed: datetime | None = None
    from_completedish = False

    if scheduled_at:
        s = str(scheduled_at).strip()
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(s[:19], fmt).replace(tzinfo=UTC)
                    break
                except ValueError:
                    continue

    if parsed is None and unix_ts is not None:
        try:
            tsv = float(unix_ts)
        except (TypeError, ValueError):
            tsv = None
        if tsv is not None:
            # Explicit: VLR unix_timestamp is Unix seconds.
            if tsv > 1e12:
                logger.warning("VLR timestamp looks like ms ({}), treating as ms", tsv)
                parsed = datetime.fromtimestamp(tsv / 1000.0, tz=UTC)
            else:
                parsed = datetime.fromtimestamp(tsv, tz=UTC)
            from_completedish = False

    if parsed is None:
        return None

    year = parsed.year
    if year < 2015 or year > next_year:
        logger.warning("VLR date out of bounds: {}", parsed.isoformat())
        return None

    if from_completedish and status == "scheduled" and not has_scores:
        _stats["date_status_anomaly"] += 1
        logger.warning("date/status anomaly on VLR match")

    return parsed.date().isoformat()


def _team_name(conn: sqlite3.Connection, team_id: int | None) -> str | None:
    if team_id is None:
        return None
    row = conn.execute("SELECT name FROM teams WHERE team_id = ?", (team_id,)).fetchone()
    if not row:
        return None
    name = (row[0] or "").strip()
    return name or None


def build_rows(db_path: str | Path) -> list[dict[str, Any]]:
    _reset_stats()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT m.match_id, m.event_id, m.team1_id, m.team2_id, m.team1_score, m.team2_score,
               m.winner_team_id, m.status, m.scheduled_at, m.unix_timestamp, m.best_of, m.url,
               e.name AS event_name
        FROM matches m
        LEFT JOIN events e ON e.event_id = m.event_id
        """
    )
    rows_out: list[dict[str, Any]] = []
    for r in cur:
        team_a = _team_name(conn, r["team1_id"])
        team_b = _team_name(conn, r["team2_id"])
        if not team_a and not team_b:
            _stats["dropped_no_teams"] += 1
            continue

        score_a = int(r["team1_score"]) if r["team1_score"] is not None else None
        score_b = int(r["team2_score"]) if r["team2_score"] is not None else None
        raw_status = r["status"]
        status = _normalize_status(raw_status, score_a, score_b)
        has_scores = score_a is not None or score_b is not None
        match_date = _parse_date(
            r["scheduled_at"], r["unix_timestamp"], status=status, has_scores=has_scores
        )

        native_id = r["match_id"]
        winner = None
        if r["winner_team_id"] is not None:
            if r["winner_team_id"] == r["team1_id"] and team_a:
                winner = team_a
            elif r["winner_team_id"] == r["team2_id"] and team_b:
                winner = team_b

        source_url = (r["url"] or "").strip() or None
        if not source_url and native_id is not None:
            # Construct from native id only (not fleet-prefixed match_id).
            source_url = f"{BASE_URL.rstrip('/')}/{native_id}/"
            _stats["url_constructed"] += 1

        fmt = f"Bo{r['best_of']}" if r["best_of"] is not None else None

        rows_out.append(
            {
                "match_id": f"{REPO_SLUG}:{native_id}",
                "match_date": match_date,
                "team_a": team_a,
                "team_b": team_b,
                "winner": winner,
                "source_url": source_url,
                "status": status,
                "score_a": score_a,
                "score_b": score_b,
                "event_name": r["event_name"],
                "format": fmt,
                "raw_status": raw_status,
            }
        )
    conn.close()
    _stats["rows_out"] = len(rows_out)
    return rows_out


def write_snapshot(db_path: str | Path, out_dir: str | Path) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = build_rows(db_path)
    if not rows:
        logger.warning("snapshot empty after filters")

    (out / "data.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "data.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in COLUMNS})
    try:
        import pandas as pd

        pd.DataFrame(rows, columns=COLUMNS).to_parquet(out / "data.parquet", index=False)
    except Exception as exc:
        logger.error("parquet export failed: {}", exc)

    manifest = {
        "source": REPO_SLUG,
        "game": GAME,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "record_count": len(rows),
        "schema_version": SCHEMA_VERSION,
        "columns": COLUMNS,
        "files": {"json": "data.json", "csv": "data.csv", "parquet": "data.parquet"},
        "stats": dict(_stats),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info(
        "snapshot {} rows (mapped={} heuristic={} dropped={})",
        len(rows),
        _stats["status_mapped"],
        _stats["status_heuristic"],
        _stats["dropped_no_teams"],
    )
    return manifest
