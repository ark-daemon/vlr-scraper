"""Teams scraper €” team page, roster, map stats, match history."""

from __future__ import annotations

import re
import traceback
from typing import Any

from loguru import logger
from selectolax.parser import HTMLParser

import vlr_scraper.queries as queries
from vlr_scraper.base import (
    AsyncScraper,
    CloudflareBlockError,
    NotFoundError,
    ScraperError,
)
from vlr_scraper.config import settings
from vlr_scraper.parser_helpers import (
    clean_text,
    extract_player_id,
    extract_team_id,
    full_url,
    parse_float,
    parse_int,
    parse_percent,
)


class TeamScraper(AsyncScraper):
    async def scrape_team_url(self, url: str, queue_id: int | None = None) -> bool:
        logger.info(f"Scraping team: {url}")
        team_id = extract_team_id(url)
        if not team_id:
            logger.warning(f"Could not extract team_id from: {url}")
            if queue_id is not None:
                await queries.queue_mark_done(queue_id)
            return False

        try:
            html = await self.get(url)
        except NotFoundError:
            logger.warning(f"Team not found (404): {url}")
            if queue_id is not None:
                await queries.queue_mark_done(queue_id)
            return False
        except CloudflareBlockError as exc:
            logger.warning(f"Cloudflare block: {url}: {exc}")
            if queue_id is not None:
                await queries.queue_retry_failed(queue_id, str(exc))
            return False
        except ScraperError as exc:
            logger.error(f"Failed to fetch team page {url}: {exc}")
            if queue_id is not None:
                await queries.queue_mark_failed(queue_id, str(exc))
            return False

        try:
            await self._parse_team_page(html, team_id, url)
        except Exception as exc:
            self._log_error(url, str(exc), traceback.format_exc(), html[:500])
            logger.error(f"Team parse failed for {url}: {exc}")
            if queue_id is not None:
                await queries.queue_mark_failed(queue_id, str(exc))
            return False

        # Team stats page
        slug = self._extract_slug(url, team_id)
        stats_url = f"{settings.BASE_URL}/team/stats/{team_id}/{slug}"
        try:
            stats_html = await self.get(stats_url)
            await self._parse_team_stats(stats_html, team_id)
        except ScraperError:
            logger.debug(f"No stats page for team {team_id}")

        if queue_id is not None:
            await queries.queue_mark_done(queue_id)

        logger.info(f"Team {team_id} scraped successfully")
        return True

    async def _parse_team_page(self, html: str, team_id: int, url: str) -> None:
        tree = HTMLParser(html)

        # Team name
        name_node = tree.css_first("h1.wf-title")
        name = clean_text(name_node.text(strip=True)) if name_node else f"Team {team_id}"

        # Abbreviation
        abbr_node = tree.css_first("div.team-header-tag")
        abbreviation = clean_text(abbr_node.text(strip=True)) if abbr_node else None

        # Logo
        logo_img = tree.css_first("div.team-header-logo img")
        logo_url = logo_img.attributes.get("src") if logo_img else None

        # Country + region from team info items
        country = None
        region = None
        for item in tree.css("div.team-header-count-content"):
            label_node = item.css_first("div.team-header-count-label")
            val_node = item.css_first("div.team-header-count-val")
            label = clean_text(label_node.text(strip=True)) if label_node else ""
            val = clean_text(val_node.text(strip=True)) if val_node else ""
            if label and "region" in label.lower():
                region = val
            elif label and "country" in label.lower():
                country = val

        # Also look for country flag
        if not country:
            flag_node = tree.css_first("div.team-header-country")
            if flag_node:
                country = clean_text(flag_node.text(strip=True))

        await queries.upsert_team(
            {
                "team_id": team_id,
                "name": name or f"Team {team_id}",
                "abbreviation": abbreviation,
                "logo_url": logo_url,
                "country": country,
                "region": region,
                "url": url,
            }
        )

        # Current roster
        player_entries: list[tuple[str, str]] = []
        roster_rows: list[dict[str, Any]] = []
        roster_player_ids: list[int] = []
        roster_items = tree.css("div.team-roster-item")
        for item in roster_items:
            player_link = item.css_first("a.team-roster-item-name-blk")
            if not player_link:
                player_link = item.css_first("a[href*='/player/']")
            if not player_link:
                continue

            href = player_link.attributes.get("href", "")
            player_id = extract_player_id(href)
            if not player_id:
                continue

            # Join date if available
            join_date_node = item.css_first("div.team-roster-item-date")
            join_date = clean_text(join_date_node.text(strip=True)) if join_date_node else None

            roster_rows.append(
                {
                    "team_id": team_id,
                    "player_id": player_id,
                    "join_date": join_date,
                    "is_current": 1,
                }
            )
            roster_player_ids.append(player_id)

            player_entries.append((full_url(href.split("?")[0]), "player"))

        # Past roster members
        past_section = tree.css_first("div.team-roster-past")
        if past_section:
            for item in past_section.css("div.team-roster-item"):
                player_link = item.css_first("a[href*='/player/']")
                if not player_link:
                    continue
                href = player_link.attributes.get("href", "")
                player_id = extract_player_id(href)
                if not player_id:
                    continue

                leave_date_node = item.css_first("div.team-roster-item-date")
                leave_date = (
                    clean_text(leave_date_node.text(strip=True)) if leave_date_node else None
                )

                roster_rows.append(
                    {
                        "team_id": team_id,
                        "player_id": player_id,
                        "leave_date": leave_date,
                        "is_current": 0,
                    }
                )
                roster_player_ids.append(player_id)
                player_entries.append((full_url(href.split("?")[0]), "player"))

        if roster_player_ids:
            await queries.ensure_players(roster_player_ids)
        if roster_rows:
            await queries.upsert_roster_entries(roster_rows)

        if player_entries:
            await queries.queue_add_many(list(set(player_entries)))

    async def _parse_team_stats(self, html: str, team_id: int) -> None:
        tree = HTMLParser(html)

        # Map stats table
        map_stats_table = tree.css_first("table.wf-table")
        if not map_stats_table:
            return

        for tr in map_stats_table.css("tbody tr"):
            tds = tr.css("td")
            if len(tds) < 8:
                continue

            def col(i: int, _tds=tds) -> str:
                return clean_text(_tds[i].text(strip=True)) or ""

            map_name = col(0)
            if not map_name:
                continue

            # Column order varies €” try to identify by header
            played = parse_int(col(1))
            won = parse_int(col(2))
            lost = parse_int(col(3))
            win_pct_str = col(4)
            win_pct = parse_percent(win_pct_str) or parse_float(win_pct_str)

            atk_rounds_played = parse_int(col(5)) if len(tds) > 5 else None
            atk_rounds_won = parse_int(col(6)) if len(tds) > 6 else None
            def_rounds_played = parse_int(col(7)) if len(tds) > 7 else None
            def_rounds_won = parse_int(col(8)) if len(tds) > 8 else None

            await queries.upsert_team_map_stats(
                {
                    "team_id": team_id,
                    "map_name": map_name,
                    "maps_played": played,
                    "maps_won": won,
                    "maps_lost": lost,
                    "win_pct": win_pct,
                    "atk_rounds_played": atk_rounds_played,
                    "atk_rounds_won": atk_rounds_won,
                    "def_rounds_played": def_rounds_played,
                    "def_rounds_won": def_rounds_won,
                }
            )

    @staticmethod
    def _extract_slug(url: str, team_id: int) -> str:
        """Extract slug from URL like /team/{id}/{slug}"""
        m = re.search(rf"/team(?:/\w+)?/{team_id}/([^/?#]+)", url)
        return m.group(1) if m else ""
