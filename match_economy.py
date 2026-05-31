"""Match economy tab parser â€” per-team summary and per-round buy details."""

from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser, Node

import queries
from parser_helpers import (
    clean_text,
    parse_bank,
    parse_int,
)

# Buy type CSS class â†’ normalized name
BUY_TYPE_CLASS_MAP = {
    "mod-eco": "eco",
    "mod-semi-eco": "semi-eco",
    "mod-semi-full": "semi-buy",
    "mod-full": "full-buy",
}


class MatchEconomyParser:
    """
    Parses the Economy tab.
    Buy type is determined from CSS class, NOT text content.
    """

    def __init__(self, html: str, match_id: int) -> None:
        self.html = html
        self.match_id = match_id
        self.tree = HTMLParser(html)

    async def parse_and_save(
        self,
        team1_id: int | None,
        team2_id: int | None,
        map_id_lookup: dict[str, int] | None = None,
    ) -> None:
        map_id_lookup = map_id_lookup or {}
        await queries.delete_economy_for_match(self.match_id)
        await queries.delete_economy_rounds_for_match(self.match_id)

        game_blocks = self.tree.css("div.vm-stats-game")
        if not game_blocks:
            game_blocks = self.tree.css("div[class*='vm-stats-game']")
        if not game_blocks:
            game_blocks = [self.tree]
        for block in game_blocks:
            game_id = getattr(block, "attributes", {}).get("data-game-id", "")
            map_play_id: int | None = None if game_id == "all" else map_id_lookup.get(game_id)
            await self._parse_game_economy(block, game_id, map_play_id, team1_id, team2_id)

    async def _parse_game_economy(
        self,
        block: Node,
        game_id: str,
        map_play_id: int | None,
        team1_id: int | None,
        team2_id: int | None,
    ) -> None:
        # Economy summary
        summary_rows = self._parse_economy_summary(block, game_id, map_play_id, team1_id, team2_id)
        if summary_rows:
            await queries.insert_economy_batch(summary_rows)

        # Per-round economy
        round_rows = self._parse_round_economy(block, game_id, map_play_id, team1_id, team2_id)
        if round_rows:
            await queries.insert_economy_rounds_batch(round_rows)

    # ------------------------------------------------------------------
    # Economy Summary (W/L per buy type per team)
    # ------------------------------------------------------------------

    def _parse_economy_summary(
        self,
        block: Node,
        game_id: str,
        map_play_id: int | None,
        team1_id: int | None,
        team2_id: int | None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        # Find the economy summary section
        # Usually a table with rows for eco, semi-eco, semi-buy, full-buy
        econ_section = block.css_first("div.vm-stats-game-economy")
        if not econ_section:
            econ_section = block.css_first("div[class*='economy']")
        if not econ_section:
            return results

        summary_table = econ_section.css_first("table.wf-table-inset")
        if not summary_table:
            summary_table = econ_section.css_first("table")
        if not summary_table:
            return results

        # Two team columns: one per team
        # Rows: eco | semi-eco | semi-buy | full-buy
        # We accumulate per-team then build records
        team_data: list[dict[str, Any]] = [
            {
                "team_id": team1_id,
                "eco_rounds_played": None,
                "eco_rounds_won": None,
                "semi_eco_rounds_played": None,
                "semi_eco_rounds_won": None,
                "semi_buy_rounds_played": None,
                "semi_buy_rounds_won": None,
                "full_buy_rounds_played": None,
                "full_buy_rounds_won": None,
            },
            {
                "team_id": team2_id,
                "eco_rounds_played": None,
                "eco_rounds_won": None,
                "semi_eco_rounds_played": None,
                "semi_eco_rounds_won": None,
                "semi_buy_rounds_played": None,
                "semi_buy_rounds_won": None,
                "full_buy_rounds_played": None,
                "full_buy_rounds_won": None,
            },
        ]

        field_map = {
            "eco": ("eco_rounds_won", "eco_rounds_played"),
            "semi-eco": ("semi_eco_rounds_won", "semi_eco_rounds_played"),
            "semi-buy": ("semi_buy_rounds_won", "semi_buy_rounds_played"),
            "full-buy": ("full_buy_rounds_won", "full_buy_rounds_played"),
        }

        for tr in summary_table.css("tbody tr"):
            tds = tr.css("td")
            if not tds:
                continue

            # First cell = buy type label
            label_text = clean_text(tds[0].text(strip=True)) or ""
            buy_type = self._label_to_buy_type(label_text)
            if not buy_type:
                continue

            won_key, played_key = field_map[buy_type]

            # Team 1 cell (index 1), Team 2 cell (index 2)
            for team_idx in range(2):
                td_idx = team_idx + 1
                if td_idx >= len(tds):
                    break
                td = tds[td_idx]
                text = clean_text(td.text(strip=True)) or ""
                won, played = self._parse_wl_fraction(text)
                team_data[team_idx][won_key] = won
                team_data[team_idx][played_key] = played

        for td_entry in team_data:
            if td_entry["team_id"]:
                results.append(
                    {
                        "match_id": self.match_id,
                        "map_play_id": map_play_id,
                        **td_entry,
                    }
                )

        return results

    # ------------------------------------------------------------------
    # Per-Round Economy
    # ------------------------------------------------------------------

    def _parse_round_economy(
        self,
        block: Node,
        game_id: str,
        map_play_id: int | None,
        team1_id: int | None,
        team2_id: int | None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        round_seen_counts: dict[int, int] = {}

        # Economy round rows: td.mod-eco-round
        round_cells = block.css("td.mod-eco-round")
        if not round_cells:
            # Try alternative selector
            round_cells = block.css("div.vm-stats-game-economy-round")
        if not round_cells:
            round_cells = block.css("td[class*='eco-round'], div[class*='economy-round']")

        for cell_idx, cell in enumerate(round_cells):
            # Round number usually from data attribute or position
            round_num = parse_int(cell.attributes.get("data-round-number"))
            if round_num is None:
                round_num = cell_idx + 1

            # Side from data attribute or CSS class
            side_attr = cell.attributes.get("data-side", "")
            if "atk" in side_attr.lower():
                side = "atk"
            elif "def" in side_attr.lower():
                side = "def"
            else:
                side = None

            # Buy type from CSS class on inner span
            buy_type = None
            inner_span = cell.css_first("span")
            if inner_span:
                classes = inner_span.attributes.get("class", "")
                buy_type = self._classes_to_buy_type(classes)

            # Bank value
            bank_span = cell.css_first("span.mod-bank")
            remaining_bank = parse_bank(
                clean_text(bank_span.text(strip=True)) if bank_span else None
            )

            # Loadout value
            loadout_span = cell.css_first("span.mod-loadout")
            loadout_value = parse_bank(
                clean_text(loadout_span.text(strip=True)) if loadout_span else None
            )

            # Round won: CSS highlight class
            cell_classes = cell.attributes.get("class", "")
            round_won = 1 if ("mod-win" in cell_classes or "won" in cell_classes) else 0

            # Team assignment: cells alternate by team rows or are grouped
            # Determine team_id from the row context
            team_id = self._get_team_id_for_cell(
                cell=cell,
                team1_id=team1_id,
                team2_id=team2_id,
                round_num=round_num,
                round_seen_counts=round_seen_counts,
            )

            results.append(
                {
                    "match_id": self.match_id,
                    "map_play_id": map_play_id,
                    "team_id": team_id,
                    "round_number": round_num,
                    "side": side,
                    "buy_type": buy_type,
                    "remaining_bank": remaining_bank,
                    "loadout_value": loadout_value,
                    "round_won": round_won,
                }
            )

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_wl_fraction(text: str) -> tuple[int | None, int | None]:
        """Parse '12 / 20' â†’ (12, 20)"""
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
        # Try single number
        n = parse_int(text)
        return n, n

    @staticmethod
    def _label_to_buy_type(label: str) -> str | None:
        label_lower = label.lower()
        if "full" in label_lower and "buy" in label_lower:
            return "full-buy"
        if "semi" in label_lower and "buy" in label_lower:
            return "semi-buy"
        if "semi" in label_lower and "eco" in label_lower:
            return "semi-eco"
        if "eco" in label_lower:
            return "eco"
        return None

    @staticmethod
    def _classes_to_buy_type(classes: str) -> str | None:
        """
        CRITICAL: buy type is a CSS class, NOT text content.
        .mod-eco â†’ eco
        .mod-semi-eco â†’ semi-eco
        .mod-semi-full â†’ semi-buy
        .mod-full â†’ full-buy
        """
        if "mod-full" in classes and "mod-semi" not in classes:
            return "full-buy"
        if "mod-semi-full" in classes:
            return "semi-buy"
        if "mod-semi-eco" in classes:
            return "semi-eco"
        if "mod-eco" in classes:
            return "eco"
        return None

    @staticmethod
    def _get_team_id_for_cell(
        cell: Node,
        team1_id: int | None,
        team2_id: int | None,
        round_num: int | None,
        round_seen_counts: dict[int, int],
    ) -> int | None:
        """
        Determine team ownership for economy round cell.
        Fallback order:
        1) Explicit team id attrs
        2) Team marker classes/attrs
        3) Per-round pair ordering (first seen cell for round -> team1, second -> team2)
        """
        attrs = getattr(cell, "attributes", {}) or {}
        team_attr = attrs.get("data-team-id") or attrs.get("data-team")
        if team_attr:
            try:
                return int(team_attr)
            except ValueError:
                pass

        class_blob = " ".join(
            [
                attrs.get("class", ""),
                attrs.get("data-side", ""),
                attrs.get("data-team", ""),
            ]
        ).lower()

        if any(token in class_blob for token in ("team1", "mod-t1", "team-a", "mod-a")):
            return team1_id
        if any(token in class_blob for token in ("team2", "mod-t2", "team-b", "mod-b")):
            return team2_id

        if round_num is not None:
            seen = round_seen_counts.get(round_num, 0)
            round_seen_counts[round_num] = seen + 1
            if seen % 2 == 0:
                return team1_id
            return team2_id

        return team1_id
