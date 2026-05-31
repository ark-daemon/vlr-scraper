"""Export utility â€” JSON, CSV, and Parquet export from SQLite."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from loguru import logger

from connection import execute_read

try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


EXPORTABLE_TABLES = [
    "events",
    "matches",
    "maps_played",
    "match_player_stats",
    "match_performance",
    "match_economy",
    "match_economy_rounds",
    "match_logs",
    "kill_matrix",
    "teams",
    "team_rosters",
    "team_map_stats",
    "players",
    "player_career_stats",
    "global_player_stats",
    "rankings",
]


async def export_table(
    table: str,
    fmt: str,
    output_dir: Path,
) -> Path:
    """Export a single table to the specified format."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = await execute_read(f"SELECT * FROM {table}")

    if not rows:
        logger.warning(f"Table '{table}' is empty, skipping export.")
        out_path = output_dir / f"{table}.{fmt}"
        out_path.touch()
        return out_path

    # Convert aiosqlite.Row â†’ list of dicts
    data: list[dict[str, Any]] = [dict(row) for row in rows]

    if fmt == "json":
        return _write_json(data, table, output_dir)
    elif fmt == "csv":
        return _write_csv(data, table, output_dir)
    elif fmt == "parquet":
        return _write_parquet(data, table, output_dir)
    else:
        raise ValueError(f"Unsupported format: {fmt}. Use json, csv, or parquet.")


def _write_json(data: list[dict], table: str, output_dir: Path) -> Path:
    out_path = output_dir / f"{table}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"Exported {len(data)} rows â†’ {out_path}")
    return out_path


def _write_csv(data: list[dict], table: str, output_dir: Path) -> Path:
    out_path = output_dir / f"{table}.csv"
    if not data:
        out_path.touch()
        return out_path
    fieldnames = list(data[0].keys())
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Exported {len(data)} rows â†’ {out_path}")
    return out_path


def _write_parquet(data: list[dict], table: str, output_dir: Path) -> Path:
    if not HAS_PANDAS:
        raise ImportError(
            "pandas and pyarrow are required for parquet export. "
            "Install with: pip install pandas pyarrow"
        )
    out_path = output_dir / f"{table}.parquet"
    df = pd.DataFrame(data)
    df.to_parquet(out_path, index=False)
    logger.info(f"Exported {len(data)} rows â†’ {out_path}")
    return out_path


async def export_all(fmt: str, output_dir: Path) -> None:
    """Export all tables to the specified format."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for table in EXPORTABLE_TABLES:
        try:
            await export_table(table, fmt, output_dir)
        except Exception as exc:
            logger.error(f"Failed to export table '{table}': {exc}")
