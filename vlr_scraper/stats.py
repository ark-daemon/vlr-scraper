"""Global stats scraper — /stats page for all regions Ã- timespans."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from selectolax.parser import HTMLParser

import vlr_scraper.queries as queries
from vlr_scraper.base import AsyncScraper, ScraperError
from vlr_scraper.config import STATS_TIMESPANS, VLR_REGIONS, settings
from vlr_scraper.parser_helpers import (
    clean_text,
    extract_player_id,
    extract_team_id,
    normalize_agent,
    parse_float,
    parse_int,
    parse_percent,
)

BASE_URL = settings.BASE_URL


class StatsScraper(AsyncScraper):
    async def scrape_all(self) -> None:
        """Scrape global stats for all 7 regions × 4 timespans = 28 requests."""
        cooldown_seconds = settings.CLOUDFLARE_COOLDOWN_MINUTES * 60
        consecutive_region_failures: dict[str, int] = {}
        skipped_regions: set[str] = set()

        for region in VLR_REGIONS + ["all"]:
            consecutive_region_failures.setdefault(region, 0)
            if region in skipped_regions:
                continue

            for timespan in STATS_TIMESPANS:
                if region in skipped_regions:
                    break
                try:
                    await self._scrape_stats_page(region=region, timespan=timespan)
                    consecutive_region_failures[region] = 0
                except Exception as exc:
                    consecutive_region_failures[region] += 1
                    logger.error(
                        f"Stats scrape failed (region={region}, timespan={timespan}): {exc}"
                    )
                    if consecutive_region_failures[region] >= 3:
                        skipped_regions.add(region)
                        logger.error(
                            f"Skipping region={region} after {consecutive_region_failures[region]} consecutive failures."
                        )
                        break
                    logger.warning(
                        f"Cooling down region={region} for {settings.CLOUDFLARE_COOLDOWN_MINUTES} minutes before retry."
                    )
                    await asyncio.sleep(cooldown_seconds)

        if skipped_regions:
            logger.warning(
                f"Skipped regions due to repeated failures: {', '.join(sorted(skipped_regions))}"
            )

    async def scrape_for_event(self, event_id: int) -> None:
        """Scrape stats filtered by a specific event."""
        url = (
            f"{BASE_URL}/stats/?event_group_id={event_id}"
            f"&region=all&country=all&min_rounds=0&min_rating=1450"
            f"&agent=all&map_id=all&timespan=all"
        )
        try:
            html = await self.get(url)
            await self._parse_stats_page(html, region="all", timespan="all", event_id=event_id)
        except ScraperError as exc:
            logger.error(f"Stats scrape for event {event_id} failed: {exc}")

    async def _scrape_stats_page(
        self,
        region: str = "all",
        timespan: str = "all",
        event_id: int | None = None,
        page: int = 1,
    ) -> None:
        current_page = page
        while current_page <= 20:
            url = self._build_stats_url(
                region=region, timespan=timespan, event_id=event_id, page=current_page
            )
            logger.info(f"Scraping stats: {url}")

            try:
                html = await self.get(url)
            except ScraperError as exc:
                logger.error(f"Failed to fetch stats page: {exc}")
                return

            rows_found = await self._parse_stats_page(
                html, region=region, timespan=timespan, event_id=event_id
            )
            # Stop when page not full (50 rows means more pages likely exist).
            if rows_found < 50:
                return
            current_page += 1

    async def _parse_stats_page(
        self,
        html: str,
        region: str,
        timespan: str,
        event_id: int | None = None,
    ) -> int:
        tree = HTMLParser(html)

        stats_tables = tree.css("table.wf-table")
        if not stats_tables:
            return 0
        stats_table = max(
            stats_tables,
            key=lambda t: len(t.css("a[href*='/player/']")),
        )
        if not stats_table.css("a[href*='/player/']"):
            return 0
        col_map = self._build_stats_col_map(stats_table)

        rows_parsed = 0
        for tr in stats_table.css("tbody tr"):
            try:
                row = self._parse_stats_row(tr, region, timespan, event_id, col_map)
                if row:
                    # Satisfy FK constraints even when player/team not scraped yet.
                    await queries.ensure_player(
                        player_id=row["player_id"],
                        url=f"{BASE_URL}/player/{row['player_id']}/",
                    )
                    if row.get("team_id"):
                        await queries.ensure_team(
                            team_id=row["team_id"],
                            url=f"{BASE_URL}/team/{row['team_id']}/",
                        )
                    await queries.upsert_global_player_stats(row)
                    rows_parsed += 1
            except Exception as exc:
                logger.debug(f"Stats row parse error: {exc}")
                continue

        logger.info(
            f"Stats page (region={region}, timespan={timespan}, event={event_id}): "
            f"{rows_parsed} rows"
        )
        return rows_parsed

    def _parse_stats_row(
        self,
        tr,
        region: str,
        timespan: str,
        event_id: int | None,
        col_map: dict[str, int],
    ) -> dict[str, Any] | None:
        tds = tr.css("td")
        if len(tds) < 10:
            return None

        def col(i: int) -> str:
            return clean_text(tds[i].text(strip=True)) or ""

        def cell_value(i: int) -> str:
            if i < 0 or i >= len(tds):
                return ""
            td = tds[i]
            for attr in ("data-sort-value", "data-value", "data-sort"):
                v = td.attributes.get(attr)
                if v:
                    return clean_text(v) or ""
            return col(i)

        def looks_like_rounds(text: str) -> bool:
            if not text:
                return False
            n = parse_int(text)
            if n is None:
                return False
            # RND is usually an integer count, often 100+ in filtered stats.
            return n >= 20 and "." not in text

        def first_idx_with(selector: str) -> int | None:
            for idx, td in enumerate(tds):
                if td.css_first(selector):
                    return idx
            return None

        player_cell_idx = first_idx_with("a[href*='/player/']")
        if player_cell_idx is None:
            return None

        def val(key: str, fallback_idx: int) -> str:
            idx = col_map.get(key, fallback_idx)
            if idx < 0 or idx >= len(tds):
                return ""
            return cell_value(idx)

        # Player ID
        player_a = tds[player_cell_idx].css_first("a[href*='/player/']")
        if not player_a:
            return None
        player_id = extract_player_id(player_a.attributes.get("href", ""))
        if not player_id:
            return None

        # Team ID
        team_a = tds[player_cell_idx].css_first("a[href*='/team/']")
        team_id: int | None = None
        if team_a:
            team_id = extract_team_id(team_a.attributes.get("href", ""))

        # Agents played can be in player cell or in adjacent dedicated agent column.
        agent_col_idx = player_cell_idx
        next_idx = player_cell_idx + 1
        if next_idx < len(tds) and tds[next_idx].css_first("img[alt]"):
            agent_col_idx = next_idx
            next_idx += 1

        agent_names: list[str] = []
        for img in tds[agent_col_idx].css("img[alt]"):
            a = normalize_agent(img.attributes.get("alt", ""))
            if a:
                agent_names.append(a)

        # Column order on VLR stats is usually:
        # Player, [Agents], [RND], Rating, ACS, K:D, KAST, ADR, KPR, APR, FKPR, FDPR, HS%, CL%
        rounds_idx = col_map.get("rounds_played")
        if rounds_idx is None and next_idx < len(tds) and looks_like_rounds(cell_value(next_idx)):
            rounds_idx = next_idx

        base = (rounds_idx + 1) if rounds_idx is not None else next_idx

        rating = parse_float(val("rating", base))
        acs = parse_float(val("acs", base + 1))

        # Rating is typically around 0.5-2.0. Some layouts encode "1.2" as "12".
        if rating is not None:
            if 5 < rating <= 30:
                rating = rating / 10.0
            elif rating > 30:
                rating = None

        kd_str = val("kd_ratio", base + 2)
        kd_ratio = None
        if "/" in kd_str:
            parts = kd_str.split("/")
            try:
                kd_ratio = float(parts[0].strip()) / float(parts[1].strip())
            except (ValueError, ZeroDivisionError):
                pass
        if kd_ratio is None:
            kd_ratio = parse_float(kd_str)

        kast = parse_percent(val("kast", base + 3))
        adr = parse_float(val("adr", base + 4))
        kpr = parse_float(val("kpr", base + 5))
        apr = parse_float(val("apr", base + 6))
        fkpr = parse_float(val("fkpr", base + 7))
        fdpr = parse_float(val("fdpr", base + 8))
        hs_pct = parse_percent(val("hs_pct", base + 9))
        cl_pct = parse_percent(val("cl_pct", base + 10))

        # Sanity checks
        if rating and rating > 10:
            logger.warning(f"Suspicious rating={rating} for player {player_id}")
        if acs and acs > 1000:
            logger.warning(f"Suspicious ACS={acs} for player {player_id}")

        return {
            "player_id": player_id,
            "team_id": team_id,
            "region": region,
            "timespan": timespan,
            "event_id": event_id,
            "agents_played": agent_names,
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
        }

    @staticmethod
    def _build_stats_col_map(stats_table) -> dict[str, int]:
        col_map: dict[str, int] = {}
        header_nodes = stats_table.css("thead th")
        if not header_nodes:
            return col_map

        for idx, th in enumerate(header_nodes):
            txt = (clean_text(th.text(strip=True)) or "").lower()
            txt = txt.replace(" ", "")
            if "rating" in txt:
                col_map["rating"] = idx
            elif txt == "acs" or "acs" in txt:
                col_map["acs"] = idx
            elif "rnd" == txt or "round" in txt:
                col_map["rounds_played"] = idx
            elif "k:d" in txt or "kd" == txt or "k/d" in txt:
                col_map["kd_ratio"] = idx
            elif "kast" in txt:
                col_map["kast"] = idx
            elif "adr" in txt:
                col_map["adr"] = idx
            elif "kpr" in txt:
                col_map["kpr"] = idx
            elif "apr" in txt:
                col_map["apr"] = idx
            elif "fkpr" in txt:
                col_map["fkpr"] = idx
            elif "fdpr" in txt:
                col_map["fdpr"] = idx
            elif "hs%" in txt or txt == "hs":
                col_map["hs_pct"] = idx
            elif "cl%" in txt or txt == "cl":
                col_map["cl_pct"] = idx
        return col_map

    @staticmethod
    def _build_stats_url(
        region: str = "all",
        timespan: str = "all",
        event_id: int | None = None,
        page: int = 1,
    ) -> str:
        event_param = event_id if event_id else "all"
        page_param = f"&page={page}" if page > 1 else ""
        return (
            f"{BASE_URL}/stats/?event_group_id={event_param}"
            f"&region={region}&country=all&min_rounds=200&min_rating=1550"
            f"&agent=all&map_id=all&timespan={timespan}{page_param}"
        )
