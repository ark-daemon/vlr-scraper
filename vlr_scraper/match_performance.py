"""Match performance tab parser €” kill matrix, multikills, and clutches."""

from __future__ import annotations

from typing import Any

from selectolax.parser import HTMLParser, Node

import vlr_scraper.queries as queries
from vlr_scraper.parser_helpers import (
    clean_text,
    extract_player_id,
    parse_int,
)


class MatchPerformanceParser:
    """
    Parses the Performance tab (kill matrix, multikills, clutches).
    Data is present in the initial HTML, hidden via CSS display:none.
    """

    def __init__(self, html: str, match_id: int) -> None:
        self.html = html
        self.match_id = match_id
        self.tree = HTMLParser(html)

    async def parse_and_save(self, map_id_lookup: dict[str, int] | None = None) -> None:
        map_id_lookup = map_id_lookup or {}
        await queries.delete_performance_for_match(self.match_id)
        await queries.delete_kill_matrix_for_match(self.match_id)

        game_blocks = self.tree.css("div.vm-stats-game")
        for block in game_blocks:
            game_id = block.attributes.get("data-game-id", "")
            map_play_id = self._resolve_map_play_id(game_id, map_id_lookup)
            await self._parse_game_performance(block, game_id, map_play_id)

    def _resolve_map_play_id(self, game_id: str, map_id_lookup: dict[str, int]) -> int | None:
        """Use overview-provided game_id -> map_play_id mapping."""
        if game_id == "all":
            return None
        return map_id_lookup.get(game_id)

    async def _parse_game_performance(
        self, block: Node, game_id: str, map_play_id: int | None
    ) -> None:
        # Kill matrix
        km_rows = self._parse_kill_matrix(block, game_id, map_play_id)
        km_player_ids = [
            row["killer_player_id"] for row in km_rows if row.get("killer_player_id")
        ] + [row["victim_player_id"] for row in km_rows if row.get("victim_player_id")]
        if km_player_ids:
            await queries.ensure_players(km_player_ids)
        if km_rows:
            await queries.insert_kill_matrix_batch(km_rows)

        # Multikills
        perf_rows, clutch_rows = self._parse_multikills_and_clutches(block, game_id, map_play_id)
        all_perf = perf_rows + clutch_rows
        if all_perf:
            perf_player_ids = [row["player_id"] for row in all_perf if row.get("player_id")]
            if perf_player_ids:
                await queries.ensure_players(perf_player_ids)
            # Merge by player_id + map_play_id
            merged = self._merge_perf_rows(all_perf)
            await queries.insert_performance_batch(merged)

    # ------------------------------------------------------------------
    # Kill Matrix (10Ã-10 grid)
    # ------------------------------------------------------------------

    def _parse_kill_matrix(
        self, block: Node, game_id: str, map_play_id: int | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        # Kill matrix rows
        matrix_rows = block.css("div.vm-stats-game-perf-matrix-row")
        if not matrix_rows:
            matrix_rows = block.css("div[class*='perf-matrix-row'], div[class*='matrix-row']")
        if not matrix_rows:
            return results

        # Collect killer player IDs from row headers
        killer_ids: list[int | None] = []
        for row in matrix_rows:
            # Player link in the row header
            player_link = row.css_first("a[href*='/player/']")
            if player_link:
                href = player_link.attributes.get("href", "")
                killer_ids.append(extract_player_id(href))
            else:
                killer_ids.append(None)

        # Collect victim player IDs from column headers (first row only)
        victim_ids: list[int | None] = []
        if matrix_rows:
            first_row = matrix_rows[0]
            col_headers = first_row.css("div.vm-stats-game-perf-matrix-col")
            if not col_headers:
                col_headers = first_row.css(
                    "div[class*='perf-matrix-col'], div[class*='matrix-col']"
                )
            # Skip the first cell (it's the row header)
            for cell in col_headers[1:]:
                player_link = cell.css_first("a[href*='/player/']")
                if player_link:
                    href = player_link.attributes.get("href", "")
                    victim_ids.append(extract_player_id(href))
                else:
                    victim_ids.append(None)

        # If no victim IDs found via col headers, extract from all rows' cells
        if not victim_ids and matrix_rows:
            # Try alternative structure: each row has cells with kill counts
            # and victim IDs may be embedded in the top header row
            header_row = block.css_first("div.vm-stats-game-perf-matrix-row.mod-header")
            if header_row:
                header_cells = header_row.css("div.vm-stats-game-perf-matrix-cell")
                if not header_cells:
                    header_cells = header_row.css(
                        "div[class*='perf-matrix-cell'], div[class*='matrix-cell']"
                    )
                for cell in header_cells:
                    player_link = cell.css_first("a[href*='/player/']")
                    if player_link:
                        href = player_link.attributes.get("href", "")
                        victim_ids.append(extract_player_id(href))
                    else:
                        victim_ids.append(None)

        # Now extract kill counts: each data row has N cells (one per victim)
        for row_idx, row in enumerate(matrix_rows):
            killer_id = killer_ids[row_idx] if row_idx < len(killer_ids) else None
            if not killer_id:
                continue

            # Data cells (skip first if it's the player name cell)
            cells = row.css("div.vm-stats-game-perf-matrix-cell")
            if not cells:
                cells = row.css("div[class*='perf-matrix-cell'], div[class*='matrix-cell']")
            # Determine offset: if first cell contains a link, it's the header
            cell_offset = 1 if (cells and cells[0].css_first("a")) else 0

            for cell_idx, cell in enumerate(cells[cell_offset:]):
                victim_id = victim_ids[cell_idx] if cell_idx < len(victim_ids) else None
                if not victim_id:
                    continue

                kill_count = parse_int(clean_text(cell.text(strip=True))) or 0

                results.append(
                    {
                        "match_id": self.match_id,
                        "map_play_id": map_play_id,
                        "killer_player_id": killer_id,
                        "victim_player_id": victim_id,
                        "kill_count": kill_count,
                    }
                )

        return results

    # ------------------------------------------------------------------
    # Multikills & Clutches tables
    # ------------------------------------------------------------------

    def _parse_multikills_and_clutches(
        self, block: Node, game_id: str, map_play_id: int | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Returns (multikill_rows, clutch_rows).
        Both reference span.mod-rnd for round numbers.
        """
        perf_rows: list[dict[str, Any]] = []
        clutch_rows: list[dict[str, Any]] = []

        # Find the performance section tables
        # Usually second and third wf-table-inset after the kill matrix section
        tables = block.css("table.wf-table-inset")

        multikill_table: Node | None = None
        clutch_table: Node | None = None

        # Identify tables by their headers
        for table in tables:
            header_text = ""
            header = table.css_first("thead")
            if header:
                header_text = clean_text(header.text(strip=True)) or ""
            if "2k" in header_text.lower() or "3k" in header_text.lower():
                multikill_table = table
            elif "1v1" in header_text or "clutch" in header_text.lower():
                clutch_table = table

        # Fallback: use position if headers not identified
        if not multikill_table and len(tables) >= 2:
            multikill_table = tables[1]
        if not clutch_table and len(tables) >= 3:
            clutch_table = tables[2]

        if multikill_table:
            perf_rows = self._parse_multikill_table(multikill_table, game_id, map_play_id)
        if clutch_table:
            clutch_rows = self._parse_clutch_table(clutch_table, game_id, map_play_id)

        return perf_rows, clutch_rows

    def _parse_multikill_table(
        self, table: Node, game_id: str, map_play_id: int | None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        for tr in table.css("tbody tr"):
            tds = tr.css("td")
            if not tds:
                continue

            # Player ID
            player_a = tds[0].css_first("a[href*='/player/']")
            if not player_a:
                continue
            player_id = extract_player_id(player_a.attributes.get("href", ""))
            if not player_id:
                continue

            def get_count_and_rounds(td: Node) -> tuple[int, list[int]]:
                count = parse_int(clean_text(td.text(strip=True))) or 0
                round_nums: list[int] = []
                for rnd_span in td.css("span.mod-rnd"):
                    rnd_text = clean_text(rnd_span.text(strip=True)) or ""
                    parsed = parse_int(rnd_text)
                    if parsed is not None:
                        round_nums.append(parsed)
                return count, round_nums

            # Column order: Player, 2K, 3K, 4K, 5K
            k2_count, k2_rounds = get_count_and_rounds(tds[1]) if len(tds) > 1 else (0, [])
            k3_count, k3_rounds = get_count_and_rounds(tds[2]) if len(tds) > 2 else (0, [])
            k4_count, k4_rounds = get_count_and_rounds(tds[3]) if len(tds) > 3 else (0, [])
            k5_count, k5_rounds = get_count_and_rounds(tds[4]) if len(tds) > 4 else (0, [])

            rows.append(
                {
                    "match_id": self.match_id,
                    "map_play_id": map_play_id,
                    "player_id": player_id,
                    "team_id": None,
                    "kills_2k": k2_count,
                    "kills_3k": k3_count,
                    "kills_4k": k4_count,
                    "kills_5k": k5_count,
                    "kills_2k_rounds": k2_rounds,
                    "kills_3k_rounds": k3_rounds,
                    "kills_4k_rounds": k4_rounds,
                    "kills_5k_rounds": k5_rounds,
                }
            )

        return rows

    def _parse_clutch_table(
        self, table: Node, game_id: str, map_play_id: int | None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        for tr in table.css("tbody tr"):
            tds = tr.css("td")
            if not tds:
                continue

            player_a = tds[0].css_first("a[href*='/player/']")
            if not player_a:
                continue
            player_id = extract_player_id(player_a.attributes.get("href", ""))
            if not player_id:
                continue

            def get_count_and_rounds(td: Node) -> tuple[int, list[int]]:
                count = parse_int(clean_text(td.text(strip=True))) or 0
                round_nums: list[int] = []
                for rnd_span in td.css("span.mod-rnd"):
                    parsed = parse_int(clean_text(rnd_span.text(strip=True)))
                    if parsed is not None:
                        round_nums.append(parsed)
                return count, round_nums

            # Column order: Player, 1v1, 1v2, 1v3, 1v4, 1v5
            v1_c, v1_r = get_count_and_rounds(tds[1]) if len(tds) > 1 else (0, [])
            v2_c, v2_r = get_count_and_rounds(tds[2]) if len(tds) > 2 else (0, [])
            v3_c, v3_r = get_count_and_rounds(tds[3]) if len(tds) > 3 else (0, [])
            v4_c, v4_r = get_count_and_rounds(tds[4]) if len(tds) > 4 else (0, [])
            v5_c, v5_r = get_count_and_rounds(tds[5]) if len(tds) > 5 else (0, [])

            rows.append(
                {
                    "match_id": self.match_id,
                    "map_play_id": map_play_id,
                    "player_id": player_id,
                    "team_id": None,
                    "clutches_v1": v1_c,
                    "clutches_v2": v2_c,
                    "clutches_v3": v3_c,
                    "clutches_v4": v4_c,
                    "clutches_v5": v5_c,
                    "clutches_v1_rounds": v1_r,
                    "clutches_v2_rounds": v2_r,
                    "clutches_v3_rounds": v3_r,
                    "clutches_v4_rounds": v4_r,
                    "clutches_v5_rounds": v5_r,
                }
            )

        return rows

    @staticmethod
    def _merge_perf_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge multikill and clutch rows by player_id + map_play_id."""
        merged: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row["player_id"], row.get("map_play_id"))
            if key not in merged:
                merged[key] = {
                    "match_id": row["match_id"],
                    "map_play_id": row.get("map_play_id"),
                    "player_id": row["player_id"],
                    "team_id": row.get("team_id"),
                    "kills_2k": 0,
                    "kills_3k": 0,
                    "kills_4k": 0,
                    "kills_5k": 0,
                    "kills_2k_rounds": [],
                    "kills_3k_rounds": [],
                    "kills_4k_rounds": [],
                    "kills_5k_rounds": [],
                    "clutches_v1": 0,
                    "clutches_v2": 0,
                    "clutches_v3": 0,
                    "clutches_v4": 0,
                    "clutches_v5": 0,
                    "clutches_v1_rounds": [],
                    "clutches_v2_rounds": [],
                    "clutches_v3_rounds": [],
                    "clutches_v4_rounds": [],
                    "clutches_v5_rounds": [],
                }
            m = merged[key]
            for field in [
                "kills_2k",
                "kills_3k",
                "kills_4k",
                "kills_5k",
                "kills_2k_rounds",
                "kills_3k_rounds",
                "kills_4k_rounds",
                "kills_5k_rounds",
                "clutches_v1",
                "clutches_v2",
                "clutches_v3",
                "clutches_v4",
                "clutches_v5",
                "clutches_v1_rounds",
                "clutches_v2_rounds",
                "clutches_v3_rounds",
                "clutches_v4_rounds",
                "clutches_v5_rounds",
            ]:
                if field in row and row[field]:
                    if isinstance(row[field], list):
                        m[field] = (m.get(field) or []) + row[field]
                    else:
                        m[field] = (m.get(field) or 0) + (row[field] or 0)

        return list(merged.values())
