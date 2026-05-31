"""Async SQLite connection management for the VLR scraper."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from loguru import logger

from config import settings

_db_lock = asyncio.Lock()
_write_lock = asyncio.Lock()
_shared_conn: aiosqlite.Connection | None = None


async def get_connection() -> aiosqlite.Connection:
    """Return shared aiosqlite connection with WAL mode and row factory."""
    global _shared_conn
    async with _db_lock:
        if _shared_conn is None:
            conn = await aiosqlite.connect(settings.DB_PATH)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA cache_size=-32000")  # 32 MB cache
            _shared_conn = conn
        return _shared_conn


@asynccontextmanager
async def db_conn() -> AsyncIterator[aiosqlite.Connection]:
    """Context manager that yields shared connection and commits on exit."""
    conn = await get_connection()
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        # Shared connection closed explicitly via close_connection().
        pass


async def close_connection() -> None:
    """Close shared connection. Safe to call multiple times."""
    global _shared_conn
    async with _db_lock:
        if _shared_conn is not None:
            await _shared_conn.close()
            _shared_conn = None


async def init_db() -> None:
    """Create all tables from schema.sql if they don't exist."""
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    async with db_conn() as conn:
        cursor = await conn.execute("PRAGMA table_info(crawl_queue)")
        column_names = {row[1] for row in await cursor.fetchall()}
        if column_names and "next_attempt_at" not in column_names:
            await conn.execute("ALTER TABLE crawl_queue ADD COLUMN next_attempt_at TEXT")
        await conn.executescript(schema_sql)

    logger.info(f"Database initialized at {settings.DB_PATH}")


async def execute_write(sql: str, params: tuple | list = ()) -> int:
    """
    Serialize all writes through a single lock to prevent SQLite contention.
    Returns lastrowid.
    """
    async with _write_lock:
        async with db_conn() as conn:
            cursor = await conn.execute(sql, params)
            return cursor.lastrowid or 0


async def execute_write_many(sql: str, params_list: list[tuple | list]) -> None:
    """Serialize a batch of writes through the write lock."""
    if not params_list:
        return
    async with _write_lock:
        async with db_conn() as conn:
            await conn.executemany(sql, params_list)


async def execute_write_returning(sql: str, params: tuple | list = ()) -> list[aiosqlite.Row]:
    async with _write_lock:
        async with db_conn() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchall()


async def execute_read(sql: str, params: tuple | list = ()) -> list[aiosqlite.Row]:
    """Execute a read query and return all rows."""
    async with db_conn() as conn:
        cursor = await conn.execute(sql, params)
        return await cursor.fetchall()


async def execute_read_one(sql: str, params: tuple | list = ()) -> aiosqlite.Row | None:
    """Execute a read query and return the first row."""
    async with db_conn() as conn:
        cursor = await conn.execute(sql, params)
        return await cursor.fetchone()
