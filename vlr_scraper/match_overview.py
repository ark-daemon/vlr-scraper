"""Match overview tab parser Ã¢‚¬" header, maps, and per-player stats."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger
from selectolax.parser import HTMLParser, Node

import vlr_scraper.queries as queries
from vlr_scraper.config import settings
from vlr_scraper.parser_helpers import (
    clean_text,
    extract_event_id,
    extract_player_id,
    extract_team_id,
    full_url,
    normalize_agent,
    parse_float,
    parse_int,
    parse_kd_diff,
    parse_percent,
)


class MatchOverviewParser:
    """
    Parses the Overview tab of a VLR match page.
    ALL tabs are in the initial HTML (server-side rendered).
    """

    def __init__(self, html: str, match_url: str) -> None:
        self.html = html
        self.url = match_url
        self.tree = HTMLParser(html)
        self.match_id: int | None = self._extract_match_id_from_url()
        self.game_id_to_map_play_id: dict[str, int] = {}

    def _extract_match_id_from_url(self) -> int | None:
        m = re.search(r"vlr\.gg/(\d+)/", self.url)
        if m:
            return int(m.group(1))
        # Try path style /(\d+)/slug
        m = re.search(r"/(\d+)/", self.url)
        return int(m.group(1)) if m else None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def parse_and_save(self) -> tuple[dict[str, Any] | None, dict[str, int]]:
        if not self.match_id:
            logger.warning(f"Could not extract match_id from URL: {self.url}")
            return None, {}

        match_data = self._parse_match_header()
        if not match_data:
            return None, {}

        await self._ensure_parent_rows(match_data)

        # Save match row
        await queries.upsert_match(match_data)

        # Delete old map/stat rows for this match (allow re-scrape)
        await queries.delete_maps_for_match(self.match_id)
        await queries.delete_player_stats_for_match(self.match_id)

        # Enqueue team URLs
        team_urls: list[tuple[str, str]] = []
        for tid_key in ("team1_id", "team2_id"):
            tid = match_data.get(tid_key)
            if tid:
                team_url = f"{settings.BASE_URL}/team/{tid}/"
                team_urls.append((team_url, "team"))
        if team_urls:
            await queries.queue_add_many(team_urls)

        # Parse each vm-stats-game block
        game_id_to_map_play_id: dict[str, int] = {}
        game_blocks = self.tree.css("div.vm-stats-game")
        map_number_counter = 0
        for block in game_blocks:
            game_id = block.attributes.get("data-game-id", "")
            if game_id != "all":
                map_number_counter += 1
            map_play_id = await self._parse_game_block(
                block,
                game_id,
                match_data,
                map_number_counter if game_id != "all" else None,
            )
            if game_id != "all" and map_play_id is not None:
                game_id_to_map_play_id[game_id] = map_play_id

        return match_data, game_id_to_map_play_id

    async def _ensure_parent_rows(self, match_data: dict[str, Any]) -> None:
        """Create minimal parent rows first so FK inserts never fail."""
        event_id = match_data.get("event_id")
        if event_id:
            await queries.ensure_event(
                event_id=event_id,
                url=f"{settings.BASE_URL}/event/{event_id}/",
            )

        team_ids = [
            tid
            for tid in (
                match_data.get("team1_id"),
                match_data.get("team2_id"),
            )
            if tid
        ]
        if team_ids:
            await queries.ensure_teams(team_ids)

    # ------------------------------------------------------------------
    # Match header
    # ------------------------------------------------------------------

    def _parse_match_header(self) -> dict[str, Any] | None:
        tree = self.tree

        # Team names and IDs from the header
        team1_id: int | None = None
        team2_id: int | None = None
        team1_score: int | None = None
        team2_score: int | None = None

        # Team links in match header
        team_links = tree.css("div.match-header-vs div.match-header-vs-team")
        if len(team_links) >= 2:
            # Team 1
            a1 = team_links[0].css_first("a")
            if a1:
                href1 = a1.attributes.get("href", "")
                team1_id = extract_team_id(href1)
            score1_node = team_links[0].css_first("div.match-header-vs-score")
            if score1_node:
                team1_score = parse_int(score1_node.text(strip=True))

            # Team 2
            a2 = team_links[1].css_first("a")
            if a2:
                href2 = a2.attributes.get("href", "")
                team2_id = extract_team_id(href2)
            score2_node = team_links[1].css_first("div.match-header-vs-score")
            if score2_node:
                team2_score = parse_int(score2_node.text(strip=True))

        # Determine winner
        winner_team_id: int | None = None
        if team1_score is not None and team2_score is not None:
            if team1_score > team2_score:
                winner_team_id = team1_id
            elif team2_score > team1_score:
                winner_team_id = team2_id

        # Event info
        event_a = tree.css_first("a.match-header-event")
        event_id: int | None = None
        event_url = ""
        if event_a:
            event_url = event_a.attributes.get("href", "")
            event_id = extract_event_id(event_url)

        # Series/stage name
        series_node = tree.css_first("div.match-header-event-series")
        series_name = clean_text(series_node.text(strip=True)) if series_node else None

        # Date/time
        date_node = tree.css_first("div.match-header-date .moment-tz-convert")
        scheduled_at: str | None = None
        unix_ts: int | None = None
        if date_node:
            scheduled_at = clean_text(date_node.text(strip=True))
            unix_ts_attr = date_node.attributes.get("data-utc-ts")
            if unix_ts_attr:
                unix_ts = parse_int(unix_ts_attr)

        # Match status
        status_node = tree.css_first("div.match-header-vs-note")
        status_text = clean_text(status_node.text(strip=True)) if status_node else ""
        if "live" in (status_text or "").lower():
            status = "live"
        elif team1_score is not None and team2_score is not None:
            status = "completed"
        else:
            status = "upcoming"

        # Best of
        best_of_node = tree.css_first("div.match-header-vs-note")
        best_of: int | None = None
        if best_of_node:
            bo_text = best_of_node.text(strip=True)
            m = re.search(r"bo(\d)", bo_text, re.IGNORECASE)
            if not m:
                m = re.search(r"best.of.(\d)", bo_text, re.IGNORECASE)
            if m:
                best_of = int(m.group(1))

        # VOD link
        vod_node = tree.css_first("a.match-streams-external")
        vod_url = vod_node.attributes.get("href") if vod_node else None

        return {
            "match_id": self.match_id,
            "event_id": event_id,
            "series_name": series_name,
            "stage_name": None,  # extracted per-match from page context
            "team1_id": team1_id,
            "team2_id": team2_id,
            "team1_score": team1_score,
            "team2_score": team2_score,
            "winner_team_id": winner_team_id,
            "status": status,
            "scheduled_at": scheduled_at,
            "unix_timestamp": unix_ts,
            "best_of": best_of,
            "vod_url": vod_url,
            "url": self.url,
        }

    # ------------------------------------------------------------------
    # Per-game block (all maps combined + each individual map)
    # ------------------------------------------------------------------

    async def _parse_game_block(
        self,
        block: Node,
        game_id: str,
        match_data: dict[str, Any],
        map_number: int | None = None,
    ) -> int | None:
        map_play_id: int | None = None

        if game_id != "all":
            # Parse map metadata
            map_data = self._parse_map_metadata(block, game_id, match_data, map_number)
            if map_data:
                map_play_id = await queries.insert_map_played(map_data)
                if map_play_id:
                    self.game_id_to_map_play_id[game_id] = map_play_id

        # Parse overview stats table
        stat_rows = self._parse_overview_table(block, match_data, map_play_id)
        if stat_rows:
            player_ids = [row["player_id"] for row in stat_rows if row.get("player_id")]
            if player_ids:
                await queries.ensure_players(player_ids)
            await queries.insert_player_stats_batch(stat_rows)
        return map_play_id

    def get_map_lookup(self) -> dict[str, int]:
        """Return game_id -> map_play_id mapping collected during overview parse."""
        return dict(self.game_id_to_map_play_id)

    def _parse_map_metadata(
        self,
        block: Node,
        game_id: str,
        match_data: dict[str, Any],
        map_number: int | None = None,
    ) -> dict[str, Any] | None:
        # Map name
        map_name_node = block.css_first("div.map span:not(.mod-sideswitch)")
        map_name = clean_text(map_name_node.text(strip=True)) if map_name_node else None
        map_name = self._clean_map_name(map_name)

        # Duration
        duration_node = block.css_first("div.map-duration")
        map_duration = clean_text(duration_node.text(strip=True)) if duration_node else None

        # Team scores per map
        # Scores visible in the score header of this game block
        score_nodes = block.css("div.score")
        team1_rounds: int | None = None
        team2_rounds: int | None = None
        if len(score_nodes) >= 2:
            team1_rounds = parse_int(score_nodes[0].text(strip=True))
            team2_rounds = parse_int(score_nodes[1].text(strip=True))

        # CT/T side splits from the score sub-items
        # Team1: first .mod-ct and .mod-t, Team2: second pair
        side_nodes = block.css("div.mod-ct, div.mod-t")
        team1_ct = team1_t = team2_ct = team2_t = None
        if len(side_nodes) >= 4:
            team1_ct = parse_int(side_nodes[0].text(strip=True))
            team1_t = parse_int(side_nodes[1].text(strip=True))
            team2_ct = parse_int(side_nodes[2].text(strip=True))
            team2_t = parse_int(side_nodes[3].text(strip=True))

        # Winner
        winner_team_id: int | None = None
        if team1_rounds is not None and team2_rounds is not None:
            if team1_rounds > team2_rounds:
                winner_team_id = match_data.get("team1_id")
            elif team2_rounds > team1_rounds:
                winner_team_id = match_data.get("team2_id")

        return {
            "match_id": match_data["match_id"],
            "map_number": map_number,
            "map_name": map_name,
            "team1_rounds": team1_rounds,
            "team2_rounds": team2_rounds,
            "team1_ct_rounds": team1_ct,
            "team1_t_rounds": team1_t,
            "team2_ct_rounds": team2_ct,
            "team2_t_rounds": team2_t,
            "winner_team_id": winner_team_id,
            "is_draw": 1 if (team1_rounds is not None and team1_rounds == team2_rounds) else 0,
            "map_duration": map_duration,
            "team1_atk_first": None,  # requires deeper parsing of round history
        }

    @staticmethod
    def _clean_map_name(map_name: str | None) -> str | None:
        if not map_name:
            return map_name
        return re.sub(
            r"(?:PICK|BAN|BANNED|LEFT|DECIDER|REMAINS)+$",
            "",
            map_name,
            flags=re.IGNORECASE,
        ).strip()

    def _parse_overview_table(
        self,
        block: Node,
        match_data: dict[str, Any],
        map_play_id: int | None,
    ) -> list[dict[str, Any]]:
        """
        Parse all wf-table-inset rows from this game block.
        Handles both teams (two separate tbody sections).
        """
        stats: list[dict[str, Any]] = []

        # Two tables Ã¢‚¬" one per team
        tables = block.css("table.wf-table-inset")

        for table_idx, table in enumerate(tables):
            # Determine team_id for this table
            if table_idx == 0:
                team_id = match_data.get("team1_id")
            else:
                team_id = match_data.get("team2_id")

            for row in table.css("tbody tr"):
                tds = row.css("td")
                if len(tds) < 13:
                    continue

                # Player ID
                player_a = tds[0].css_first("a")
                if not player_a:
                    continue
                href = player_a.attributes.get("href", "")
                player_id = extract_player_id(href)
                if not player_id:
                    continue

                # Agent
                agent_img = tds[1].css_first("img")
                agent_raw = agent_img.attributes.get("alt") if agent_img else None
                agent = normalize_agent(agent_raw)

                def col_text(i: int, _tds=tds) -> str:
                    td = _tds[i]
                    # Prefer explicit sortable/value attributes to avoid merged hidden text.
                    for attr in ("data-sort-value", "data-value", "data-sort"):
                        v = clean_text(td.attributes.get(attr))
                        if v:
                            return v
                    # Prefer a single visible stat token when present.
                    stat_node = td.css_first("span.mod-both, span.mod-stat")
                    if stat_node:
                        stat_text = clean_text(stat_node.text(strip=True))
                        if stat_text:
                            return stat_text
                    return clean_text(td.text(strip=True)) or ""

                acs_val = parse_float(col_text(3))
                adr_val = parse_float(col_text(9))

                # Sanity check: detect concatenated / hidden-element garbage
                if acs_val is not None and acs_val > 10000:
                    logger.warning(
                        f"ACS sanity fail: {acs_val} for player {player_id} "
                        f"match {match_data['match_id']}. Raw td HTML: {tds[3].html}"
                    )
                if adr_val is not None and adr_val > 10000:
                    logger.warning(
                        f"ADR sanity fail: {adr_val} for player {player_id} "
                        f"match {match_data['match_id']}. Raw td HTML: {tds[9].html}"
                    )

                stats.append(
                    {
                        "match_id": match_data["match_id"],
                        "map_play_id": map_play_id,
                        "player_id": player_id,
                        "team_id": team_id,
                        "agent": agent,
                        "rating": parse_float(col_text(2)),
                        "acs": acs_val,
                        "kills": parse_int(col_text(4)),
                        "deaths": parse_int(col_text(5)),
                        "assists": parse_int(col_text(6)),
                        "kd_diff": parse_kd_diff(col_text(7)),
                        "kast": parse_percent(col_text(8)),
                        "adr": adr_val,
                        "hs_pct": parse_percent(col_text(10)),
                        "fk": parse_int(col_text(11)),
                        "fd": parse_int(col_text(12)),
                        "fk_diff": parse_kd_diff(col_text(13)) if len(tds) > 13 else None,
                        "rounds_played": None,
                    }
                )

        return stats

    async def enqueue_player_urls(self) -> None:
        """Collect all player hrefs from the page and add to crawl queue."""
        player_entries: list[tuple[str, str]] = []
        for a_tag in self.tree.css("a[href*='/player/']"):
            href = a_tag.attributes.get("href", "")
            pid = extract_player_id(href)
            if pid:
                player_entries.append((full_url(href.split("?")[0]), "player"))
        unique = list(set(player_entries))
        if unique:
            await queries.queue_add_many(unique)
