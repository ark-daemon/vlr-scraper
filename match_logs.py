"""Match logs tab parser â€” round-by-round kill/plant/defuse feed."""

from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser, Node

import queries
from parser_helpers import (
    clean_text,
    extract_player_id,
    parse_int,
)


class MatchLogsParser:
    """
    Parses the Logs tab for all rounds and events.
    Data is present in the initial HTML (server-side rendered).
    """

    def __init__(self, html: str, match_id: int) -> None:
        self.html = html
        self.match_id = match_id
        self.tree = HTMLParser(html)

    async def parse_and_save(self, map_id_lookup: dict[str, int] | None = None) -> None:
        map_id_lookup = map_id_lookup or {}
        await queries.delete_logs_for_match(self.match_id)

        game_blocks = self.tree.css("div.vm-stats-game")
        if not game_blocks:
            game_blocks = self.tree.css("div[class*='vm-stats-game']")
        if not game_blocks:
            game_blocks = [self.tree]
        for block in game_blocks:
            game_id = getattr(block, "attributes", {}).get("data-game-id", "")
            map_play_id: int | None = None if game_id == "all" else map_id_lookup.get(game_id)
            log_rows = self._parse_game_logs(block, game_id, map_play_id)
            if log_rows:
                await queries.insert_logs_batch(log_rows)

    def _parse_game_logs(
        self, block: Node, game_id: str, map_play_id: int | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        # Each round has a container div
        round_divs = block.css("div.vm-stats-game-logs-round")
        if not round_divs:
            # Try alternative: look for round sections in the logs tab
            round_divs = block.css("div.vlr-rounds-row")
        if not round_divs:
            round_divs = block.css(
                "div[class*='logs-round'], div[class*='round-log'], div[class*='log-round']"
            )

        for round_div in round_divs:
            # Extract round number from round header
            round_num = self._extract_round_number(round_div)
            if round_num is None:
                continue

            # Extract round winner team ID
            round_winner_team = self._extract_round_winner(round_div)
            round_end_reason = self._extract_round_end_reason(round_div)

            # Each event within the round
            event_divs = round_div.css("div.vm-stats-game-logs-event")
            if not event_divs:
                event_divs = round_div.css("div.log-row")
            if not event_divs:
                event_divs = round_div.css(
                    "div[class*='logs-event'], div[class*='log-event'], div[class*='log-item']"
                )

            for event_idx, event_div in enumerate(event_divs):
                event = self._parse_log_event(
                    event_div,
                    event_idx,
                    round_num,
                    map_play_id,
                    round_winner_team,
                    round_end_reason,
                )
                if event:
                    results.append(event)

            # If no events found but round_div itself is an event row
            if not event_divs:
                event = self._parse_log_event(
                    round_div,
                    0,
                    round_num,
                    map_play_id,
                    round_winner_team,
                    round_end_reason,
                )
                if event:
                    results.append(event)

        return results

    def _extract_round_number(self, round_div: Node) -> int | None:
        # Look for round number in header elements
        header = round_div.css_first(
            "div.vm-stats-game-logs-round-header, div.round-header, span.round-num"
        )
        if header:
            text = clean_text(header.text(strip=True)) or ""
            m = re.search(r"(\d+)", text)
            if m:
                return int(m.group(1))

        # Try data attribute
        data_round = round_div.attributes.get("data-round")
        if data_round:
            return parse_int(data_round)

        # Try class pattern
        classes = round_div.attributes.get("class") or ""
        m = re.search(r"round-(\d+)", classes)
        if m:
            return int(m.group(1))

        return None

    def _extract_round_winner(self, round_div: Node) -> int | None:
        # Win indicator often has team ID
        win_node = round_div.css_first("[data-winner-team]")
        if win_node:
            return parse_int(win_node.attributes.get("data-winner-team"))

        # Try CSS class on round outcome
        classes = round_div.attributes.get("class") or ""
        if "mod-win" in classes or "team1-win" in classes:
            return None  # Would need match context to resolve team ID

        return None

    def _extract_round_end_reason(self, round_div: Node) -> str | None:
        # Round end reason from data attribute or icon class
        reason_node = round_div.css_first("[data-round-end-reason]")
        if reason_node:
            return reason_node.attributes.get("data-round-end-reason")

        # Icon-based detection
        if round_div.css_first("img.round-end-spike-detonate"):
            return "spike_detonated"
        if round_div.css_first("img.round-end-spike-defuse"):
            return "spike_defused"
        if round_div.css_first("img.round-end-eliminated"):
            return "eliminated"
        if round_div.css_first("img.round-end-time"):
            return "time_expired"

        return None

    def _parse_log_event(
        self,
        event_div: Node,
        event_idx: int,
        round_num: int,
        map_play_id: int | None,
        round_winner_team: int | None,
        round_end_reason: str | None,
    ) -> dict[str, Any] | None:
        classes = event_div.attributes.get("class", "")

        # Determine event type
        event_type = self._determine_event_type(event_div, classes)
        if not event_type:
            return None

        result: dict[str, Any] = {
            "match_id": self.match_id,
            "map_play_id": map_play_id,
            "round_number": round_num,
            "event_order": event_idx,
            "event_type": event_type,
            "killer_player_id": None,
            "victim_player_id": None,
            "weapon": None,
            "is_headshot": None,
            "is_wallbang": None,
            "spike_planted_by": None,
            "round_winner_team": round_winner_team,
            "round_end_reason": round_end_reason,
        }

        if event_type == "kill":
            self._parse_kill_event(event_div, result)
        elif event_type == "plant":
            self._parse_plant_event(event_div, result)
        elif event_type in ("defuse", "detonate"):
            self._parse_plant_event(event_div, result)

        return result

    def _determine_event_type(self, event_div: Node, classes: str) -> str | None:
        # Check CSS classes for event type
        if "mod-kill" in classes or "kill" in classes:
            return "kill"
        if "mod-plant" in classes or "plant" in classes:
            return "plant"
        if "mod-defuse" in classes or "defuse" in classes:
            return "defuse"
        if "mod-detonate" in classes or "detonate" in classes:
            return "detonate"

        # Check data attribute
        event_type_attr = event_div.attributes.get("data-event-type", "")
        if event_type_attr in ("kill", "plant", "defuse", "detonate"):
            return event_type_attr

        # Check for weapon image (indicates kill)
        if event_div.css_first("img.weapon, img[class*='weapon']"):
            return "kill"

        # Check for spike icon
        if event_div.css_first("img[class*='spike']"):
            text = clean_text(event_div.text(strip=True)) or ""
            if "defuse" in text.lower():
                return "defuse"
            return "plant"

        return None

    def _parse_kill_event(self, event_div: Node, result: dict[str, Any]) -> None:
        # Killer â€” usually first player link
        player_links = event_div.css("a[href*='/player/']")
        if len(player_links) >= 1:
            href1 = player_links[0].attributes.get("href", "")
            result["killer_player_id"] = extract_player_id(href1)
        if len(player_links) >= 2:
            href2 = player_links[1].attributes.get("href", "")
            result["victim_player_id"] = extract_player_id(href2)

        # Weapon from image alt or data attribute
        weapon_img = event_div.css_first("img.weapon, img[class*='weapon']")
        if weapon_img:
            result["weapon"] = (
                weapon_img.attributes.get("alt")
                or weapon_img.attributes.get("title")
                or weapon_img.attributes.get("data-weapon")
            )

        # Headshot indicator
        classes = event_div.attributes.get("class", "")
        result["is_headshot"] = 1 if "headshot" in classes or "mod-hs" in classes else 0

        # Wallbang indicator
        result["is_wallbang"] = 1 if "wallbang" in classes or "mod-wb" in classes else 0

    def _parse_plant_event(self, event_div: Node, result: dict[str, Any]) -> None:
        player_link = event_div.css_first("a[href*='/player/']")
        if player_link:
            href = player_link.attributes.get("href", "")
            planter_id = extract_player_id(href)
            result["spike_planted_by"] = planter_id
            result["killer_player_id"] = planter_id
