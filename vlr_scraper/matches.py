"""
Match scraper orchestrator.
Fetches a match page ONCE and dispatches to all 4 tab parsers.
All tab data is server-side rendered in the initial HTML.
"""

from __future__ import annotations

import traceback
from typing import Any

from loguru import logger

import vlr_scraper.queries as queries
from vlr_scraper.base import (
    AsyncScraper,
    CloudflareBlockError,
    NotFoundError,
    ScraperError,
)
from vlr_scraper.config import settings
from vlr_scraper.match_economy import MatchEconomyParser
from vlr_scraper.match_logs import MatchLogsParser
from vlr_scraper.match_overview import MatchOverviewParser
from vlr_scraper.match_performance import MatchPerformanceParser
from vlr_scraper.parser_helpers import extract_match_id


class MatchScraper(AsyncScraper):
    async def scrape_match_url(self, url: str, queue_id: int | None = None) -> bool:
        """
        Fetch and parse a single match page.
        Returns True on success, False on failure.
        """
        logger.info(f"Scraping match: {url}")

        try:
            html = await self.get(url)
        except NotFoundError:
            logger.warning(f"Match not found (404): {url}")
            if queue_id is not None:
                await queries.queue_mark_done(queue_id)
            return False
        except CloudflareBlockError as exc:
            logger.warning(f"Cloudflare block: {url}: {exc}")
            if queue_id is not None:
                await queries.queue_retry_failed(queue_id, str(exc))
            return False
        except ScraperError as exc:
            logger.error(f"Failed to fetch match {url}: {exc}")
            self._log_error(url, str(exc), traceback.format_exc())
            if queue_id is not None:
                await queries.queue_mark_failed(queue_id, str(exc))
            return False

        match_id = extract_match_id(url)
        if not match_id:
            logger.warning(f"Could not extract match_id from: {url}")
            if queue_id is not None:
                await queries.queue_mark_done(queue_id)
            return False
        await queries.ensure_match_stub(match_id=match_id, url=url)

        # ----------------------------------------------------------------
        # Overview (header + per-map stats)
        # ----------------------------------------------------------------
        match_data: dict[str, Any] | None = None
        map_id_lookup: dict[str, int] = {}
        try:
            overview = MatchOverviewParser(html, url)
            parsed = await overview.parse_and_save()
            if isinstance(parsed, tuple):
                match_data, map_id_lookup = parsed
            else:
                # Backward compatibility if parser returns only match_data.
                match_data = parsed
                map_id_lookup = overview.get_map_lookup()
            await overview.enqueue_player_urls()
        except Exception as exc:
            self._log_error(
                url, f"Overview parse error: {exc}", traceback.format_exc(), html[:1000]
            )
            logger.error(f"Overview parse failed for {url}: {exc}")
            # Continue to other tabs even if overview fails
        if not match_data:
            logger.warning(f"Match page parsed without overview data: {url}")
        if not map_id_lookup:
            logger.warning(f"Match page parsed without map rows: {url}")

        team1_id = match_data.get("team1_id") if match_data else None
        team2_id = match_data.get("team2_id") if match_data else None

        # ----------------------------------------------------------------
        # Performance tab
        # ----------------------------------------------------------------
        try:
            perf_parser = MatchPerformanceParser(html, match_id)
            await perf_parser.parse_and_save(map_id_lookup=map_id_lookup)
        except Exception as exc:
            self._log_error(url, f"Performance parse error: {exc}", traceback.format_exc(), "")
            logger.warning(f"Performance parse failed for match {match_id}: {exc}")

        # ----------------------------------------------------------------
        # Economy tab
        # ----------------------------------------------------------------
        try:
            econ_parser = MatchEconomyParser(html, match_id)
            await econ_parser.parse_and_save(
                team1_id=team1_id,
                team2_id=team2_id,
                map_id_lookup=map_id_lookup,
            )
        except Exception as exc:
            self._log_error(url, f"Economy parse error: {exc}", traceback.format_exc(), "")
            logger.warning(f"Economy parse failed for match {match_id}: {exc}")

        # ----------------------------------------------------------------
        # Logs tab
        # ----------------------------------------------------------------
        try:
            logs_parser = MatchLogsParser(html, match_id)
            await logs_parser.parse_and_save(map_id_lookup=map_id_lookup)
        except Exception as exc:
            self._log_error(url, f"Logs parse error: {exc}", traceback.format_exc(), "")
            logger.warning(f"Logs parse failed for match {match_id}: {exc}")

        if queue_id is not None:
            await queries.queue_mark_done(queue_id)

        logger.info(f"Match {match_id} scraped successfully")
        return True

    async def scrape_match_id(self, match_id: int, slug: str = "") -> bool:
        """Convenience method to scrape by match ID."""
        url = f"{settings.BASE_URL}/{match_id}/{slug}"
        return await self.scrape_match_url(url)
