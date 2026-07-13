"""Rankings scraper €” /rankings/{region} for all 7 regions."""

from __future__ import annotations

from typing import Any

from loguru import logger
from selectolax.parser import HTMLParser

import vlr_scraper.queries as queries
from vlr_scraper.base import AsyncScraper, ScraperError
from vlr_scraper.config import RANKINGS_REGIONS, settings
from vlr_scraper.parser_helpers import (
    clean_text,
    extract_team_id,
    full_url,
    parse_int,
)

BASE_URL = settings.BASE_URL


class RankingsScraper(AsyncScraper):
    async def scrape_all(self) -> None:
        """Scrape rankings for all regions."""
        for region in RANKINGS_REGIONS:
            try:
                await self._scrape_region(region)
            except Exception as exc:
                logger.error(f"Rankings scrape failed for region={region}: {exc}")

    async def _scrape_region(self, region: str) -> None:
        url = f"{BASE_URL}/rankings/{region}"
        logger.info(f"Scraping rankings: {url}")

        try:
            html = await self.get(url)
        except ScraperError as exc:
            logger.error(f"Failed to fetch rankings {url}: {exc}")
            return

        rows = self._parse_rankings(html, region)
        for row in rows:
            try:
                await queries.ensure_team(
                    team_id=row["team_id"],
                    url=row.get("team_url"),
                )
                await queries.upsert_ranking(row)
            except Exception as exc:
                logger.debug(f"Rankings upsert error: {exc}")

        # Enqueue team URLs found
        team_urls: list[tuple[str, str]] = []
        for row in rows:
            if row.get("team_url"):
                team_urls.append((row["team_url"], "team"))
        if team_urls:
            await queries.queue_add_many(list(set(team_urls)))

        logger.info(f"Rankings {region}: {len(rows)} teams")

    def _parse_rankings(self, html: str, region: str) -> list[dict[str, Any]]:
        tree = HTMLParser(html)
        results: list[dict[str, Any]] = []

        # Rankings table
        rankings_table = tree.css_first("table.wf-table")
        if not rankings_table:
            # Try card-based layout
            return self._parse_rankings_cards(tree, region)

        for tr in rankings_table.css("tbody tr"):
            tds = tr.css("td")
            if not tds:
                continue

            def col(i: int, _tds=tds) -> str:
                return clean_text(_tds[i].text(strip=True)) or ""

            # Rank
            rank = parse_int(col(0))

            # Team
            team_a = tr.css_first("a[href*='/team/']")
            team_id: int | None = None
            team_url: str | None = None
            if team_a:
                href = team_a.attributes.get("href", "")
                team_id = extract_team_id(href)
                team_url = full_url(href.split("?")[0]) if href else None

            if not team_id:
                continue

            # Record (W-L)
            record = col(2) if len(tds) > 2 else None

            # Earnings
            earnings = col(3) if len(tds) > 3 else None

            results.append(
                {
                    "team_id": team_id,
                    "region": region,
                    "rank": rank,
                    "record": record,
                    "earnings": earnings,
                    "team_url": team_url,
                }
            )

        return results

    def _parse_rankings_cards(self, tree, region: str) -> list[dict[str, Any]]:
        """Fallback for card-based ranking layouts."""
        results: list[dict[str, Any]] = []

        rank_counter = 1
        for card in tree.css("div.rank-item, div.rankings-item"):
            team_a = card.css_first("a[href*='/team/']")
            if not team_a:
                continue

            href = team_a.attributes.get("href", "")
            team_id = extract_team_id(href)
            if not team_id:
                continue

            rank_node = card.css_first("div.rank-item-rank, span.rank")
            rank = (
                parse_int(clean_text(rank_node.text(strip=True)) if rank_node else "")
                or rank_counter
            )

            record_node = card.css_first("div.rank-item-record")
            record = clean_text(record_node.text(strip=True)) if record_node else None

            earnings_node = card.css_first("div.rank-item-earnings")
            earnings = clean_text(earnings_node.text(strip=True)) if earnings_node else None

            results.append(
                {
                    "team_id": team_id,
                    "region": region,
                    "rank": rank,
                    "record": record,
                    "earnings": earnings,
                    "team_url": full_url(href.split("?")[0]),
                }
            )
            rank_counter += 1

        return results
