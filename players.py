"""Players scraper â€” bio, agent career stats, event breakdowns."""

from __future__ import annotations

import re
import traceback
from typing import Any

from loguru import logger
from selectolax.parser import HTMLParser

import queries
from base import (
    AsyncScraper,
    CloudflareBlockError,
    NotFoundError,
    ScraperError,
)
from config import settings
from parser_helpers import (
    clean_text,
    extract_event_id,
    extract_player_id,
    extract_team_id,
    normalize_agent,
    parse_float,
    parse_int,
    parse_percent,
)


class PlayerScraper(AsyncScraper):
    async def scrape_player_url(self, url: str, queue_id: int | None = None) -> bool:
        logger.info(f"Scraping player: {url}")
        player_id = extract_player_id(url)
        if not player_id:
            logger.warning(f"Could not extract player_id from: {url}")
            if queue_id is not None:
                await queries.queue_mark_done(queue_id)
            return False

        try:
            html = await self.get(url)
        except NotFoundError:
            if queue_id is not None:
                await queries.queue_mark_done(queue_id)
            return False
        except CloudflareBlockError as exc:
            logger.warning(f"Cloudflare block: {url}: {exc}")
            if queue_id is not None:
                await queries.queue_retry_failed(queue_id, str(exc))
            return False
        except ScraperError as exc:
            self._log_error(url, str(exc), traceback.format_exc())
            if queue_id is not None:
                await queries.queue_mark_failed(queue_id, str(exc))
            return False

        try:
            await self._parse_player_page(html, player_id, url)
        except Exception as exc:
            self._log_error(url, str(exc), traceback.format_exc(), html[:500])
            logger.error(f"Player parse failed for {url}: {exc}")
            if queue_id is not None:
                await queries.queue_mark_failed(queue_id, str(exc))
            return False

        if queue_id is not None:
            await queries.queue_mark_done(queue_id)
        logger.info(f"Player {player_id} scraped successfully")
        return True

    async def _parse_player_page(self, html: str, player_id: int, url: str) -> None:
        tree = HTMLParser(html)

        # ----------------------------------------------------------------
        # Bio
        # ----------------------------------------------------------------
        ign_node = tree.css_first("h1.wf-title")
        ign = clean_text(ign_node.text(strip=True)) if ign_node else f"Player{player_id}"

        real_name_node = tree.css_first("h2.player-real-name")
        real_name = clean_text(real_name_node.text(strip=True)) if real_name_node else None

        country_node = tree.css_first("div.ge-text-light")
        country = clean_text(country_node.text(strip=True)) if country_node else None

        flag_img = tree.css_first("i.flag")
        country_flag = (
            (flag_img.attributes.get("class") or "").replace("flag ", "").strip()
            if flag_img
            else None
        )

        # Current team
        current_team_id: int | None = None
        team_link = tree.css_first("a[href*='/team/']")
        if team_link:
            href = team_link.attributes.get("href", "")
            current_team_id = extract_team_id(href)
            if current_team_id:
                await queries.ensure_team(
                    team_id=current_team_id,
                    url=f"{settings.BASE_URL}/team/{current_team_id}/",
                )

        # Social links
        twitter: str | None = None
        twitch: str | None = None
        for social_a in tree.css("a[href*='twitter.com'], a[href*='x.com']"):
            twitter = social_a.attributes.get("href")
            break
        for social_a in tree.css("a[href*='twitch.tv']"):
            twitch = social_a.attributes.get("href")
            break

        await queries.upsert_player(
            {
                "player_id": player_id,
                "ign": ign,
                "real_name": real_name,
                "country": country,
                "country_flag": country_flag,
                "current_team_id": current_team_id,
                "twitter": twitter,
                "twitch": twitch,
                "url": url,
            }
        )

        # ----------------------------------------------------------------
        # Agent career stats (first wf-table)
        # ----------------------------------------------------------------
        tables = tree.css("table.wf-table")
        if tables:
            agent_rows = self._parse_agent_stats_table(tables[0], player_id)
            for row in agent_rows:
                await queries.upsert_player_career_stats(row)

        # ----------------------------------------------------------------
        # Event breakdown (second wf-table)
        # ----------------------------------------------------------------
        if len(tables) >= 2:
            event_rows = self._parse_event_stats_table(tables[1], player_id)
            for row in event_rows:
                event_id = row.get("event_id")
                if event_id:
                    await queries.ensure_event(
                        event_id=event_id,
                        url=f"{settings.BASE_URL}/event/{event_id}/",
                    )
            for row in event_rows:
                await queries.upsert_player_career_stats(row)

    def _parse_agent_stats_table(self, table, player_id: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        for tr in table.css("tbody tr"):
            tds = tr.css("td")
            if len(tds) < 10:
                continue

            def col(i: int, _tds=tds) -> str:
                return clean_text(_tds[i].text(strip=True)) or ""

            # First column: agent image + name
            agent_img = tds[0].css_first("img")
            if agent_img:
                agent_raw = agent_img.attributes.get("alt") or col(0)
            else:
                agent_raw = col(0)
            agent = normalize_agent(agent_raw)

            # Column order:
            # [0]=Agent [1]=Usage% [2]=RND [3]=Rating [4]=ACS [5]=K:D
            # [6]=KAST [7]=ADR [8]=KPR [9]=APR [10]=FKPR [11]=FDPR
            # [12]=HS% [13]=CL% [14]=CL
            maps_played = parse_int(col(1))
            rounds = parse_int(col(2))
            rating = parse_float(col(3))
            acs = parse_float(col(4))
            kd_str = col(5)
            kd_ratio = self._parse_kd_ratio(kd_str)
            kast = parse_percent(col(6))
            adr = parse_float(col(7))
            kpr = parse_float(col(8))
            apr = parse_float(col(9))
            fkpr = parse_float(col(10)) if len(tds) > 10 else None
            fdpr = parse_float(col(11)) if len(tds) > 11 else None
            hs_pct = parse_percent(col(12)) if len(tds) > 12 else None
            cl_pct = parse_percent(col(13)) if len(tds) > 13 else None

            # CL Won/Total from "2 / 5" format
            cl_text = col(14) if len(tds) > 14 else ""
            cl_won, cl_played = self._parse_fraction(cl_text)

            rows.append(
                {
                    "player_id": player_id,
                    "event_id": None,
                    "agent": agent,
                    "maps_played": maps_played,
                    "rounds_played": rounds,
                    "rating": rating,
                    "acs": acs,
                    "kd_ratio": kd_ratio,
                    "kast": kast,
                    "adr": adr,
                    "kpr": kpr,
                    "apr": apr,
                    "fkpr": fkpr,
                    "fdpr": fdpr,
                    "hs_pct": hs_pct,
                    "cl_pct": cl_pct,
                    "cl_won": cl_won,
                    "cl_played": cl_played,
                }
            )

        return rows

    def _parse_event_stats_table(self, table, player_id: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        for tr in table.css("tbody tr"):
            tds = tr.css("td")
            if len(tds) < 5:
                continue

            # Event link
            event_a = tds[0].css_first("a[href*='/event/']")
            event_id: int | None = None
            if event_a:
                href = event_a.attributes.get("href", "")
                event_id = extract_event_id(href)

            def col(i: int, _tds=tds) -> str:
                return clean_text(_tds[i].text(strip=True)) or ""

            maps_played = parse_int(col(1))
            rounds = parse_int(col(2))
            rating = parse_float(col(3))
            acs = parse_float(col(4))
            kd_ratio = self._parse_kd_ratio(col(5)) if len(tds) > 5 else None
            kast = parse_percent(col(6)) if len(tds) > 6 else None
            adr = parse_float(col(7)) if len(tds) > 7 else None
            kpr = parse_float(col(8)) if len(tds) > 8 else None
            apr = parse_float(col(9)) if len(tds) > 9 else None
            fkpr = parse_float(col(10)) if len(tds) > 10 else None
            fdpr = parse_float(col(11)) if len(tds) > 11 else None
            hs_pct = parse_percent(col(12)) if len(tds) > 12 else None
            cl_pct = parse_percent(col(13)) if len(tds) > 13 else None

            rows.append(
                {
                    "player_id": player_id,
                    "event_id": event_id,
                    "agent": None,
                    "maps_played": maps_played,
                    "rounds_played": rounds,
                    "rating": rating,
                    "acs": acs,
                    "kd_ratio": kd_ratio,
                    "kast": kast,
                    "adr": adr,
                    "kpr": kpr,
                    "apr": apr,
                    "fkpr": fkpr,
                    "fdpr": fdpr,
                    "hs_pct": hs_pct,
                    "cl_pct": cl_pct,
                    "cl_won": None,
                    "cl_played": None,
                }
            )

        return rows

    @staticmethod
    def _parse_kd_ratio(text: str) -> float | None:
        if not text:
            return None
        # "1.23" or "1.23 / 0.98"
        m = re.match(r"[\d.]+", text.strip())
        if m:
            try:
                return float(m.group())
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_fraction(text: str) -> tuple[int | None, int | None]:
        """Parse '2 / 5' â†’ (2, 5)"""
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
        n = parse_int(text)
        return n, n
