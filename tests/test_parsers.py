"""
Test suite for VLR scraper parsers.
Run with: python -m pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vlr_scraper.match_economy import MatchEconomyParser
from vlr_scraper.match_logs import MatchLogsParser
from vlr_scraper.match_overview import MatchOverviewParser
from vlr_scraper.match_performance import MatchPerformanceParser
from vlr_scraper.parser_helpers import (
    clean_text,
    extract_event_id,
    extract_match_id,
    extract_player_id,
    extract_team_id,
    normalize_agent,
    parse_bank,
    parse_float,
    parse_int,
    parse_kd_diff,
    parse_percent,
)

# ---------------------------------------------------------------------------
# parser_helpers tests
# ---------------------------------------------------------------------------


class TestCleanText:
    def test_strips_whitespace(self):
        assert clean_text("  hello  ") == "hello"

    def test_collapses_internal_whitespace(self):
        assert clean_text("hello   world") == "hello world"

    def test_none_input(self):
        assert clean_text(None) is None

    def test_empty_string(self):
        assert clean_text("   ") is None


class TestParseInt:
    def test_basic(self):
        assert parse_int("42") == 42

    def test_with_commas(self):
        assert parse_int("1,234") == 1234

    def test_negative(self):
        assert parse_int("-5") == -5

    def test_none(self):
        assert parse_int(None) is None

    def test_non_numeric(self):
        assert parse_int("N/A") is None

    def test_with_suffix(self):
        assert parse_int("42%") == 42


class TestParseFloat:
    def test_basic(self):
        assert parse_float("1.23") == pytest.approx(1.23)

    def test_integer_string(self):
        assert parse_float("5") == pytest.approx(5.0)

    def test_none(self):
        assert parse_float(None) is None

    def test_non_numeric(self):
        assert parse_float("abc") is None


class TestParsePercent:
    def test_with_pct_sign(self):
        assert parse_percent("72%") == pytest.approx(0.72)

    def test_without_pct_sign(self):
        assert parse_percent("72") == pytest.approx(0.72)

    def test_zero(self):
        assert parse_percent("0%") == pytest.approx(0.0)

    def test_hundred(self):
        assert parse_percent("100%") == pytest.approx(1.0)

    def test_none(self):
        assert parse_percent(None) is None


class TestParseBank:
    def test_with_dollar_and_comma(self):
        assert parse_bank("$3,800") == 3800

    def test_plain_number(self):
        assert parse_bank("4200") == 4200

    def test_zero(self):
        assert parse_bank("$0") == 0

    def test_none(self):
        assert parse_bank(None) is None


class TestParseKdDiff:
    def test_positive(self):
        assert parse_kd_diff("+12") == 12

    def test_negative(self):
        assert parse_kd_diff("-5") == -5

    def test_zero(self):
        assert parse_kd_diff("0") == 0

    def test_none(self):
        assert parse_kd_diff(None) is None


class TestExtractIds:
    def test_player_id(self):
        assert extract_player_id("/player/12345/aspas") == 12345

    def test_team_id(self):
        assert extract_team_id("/team/6789/sentinels") == 6789

    def test_event_id(self):
        assert extract_event_id("/event/1001/vct-2024") == 1001

    def test_match_id_path(self):
        assert extract_match_id("/98765/navi-vs-sen") == 98765

    def test_match_id_full_url(self):
        assert extract_match_id("https://www.vlr.gg/98765/navi-vs-sen") == 98765

    def test_no_match(self):
        assert extract_player_id("/team/123/") is None
        assert extract_team_id("/player/456/") is None


class TestNormalizeAgent:
    def test_canonical_unchanged(self):
        assert normalize_agent("Jett") == "Jett"

    def test_kayo_alias(self):
        assert normalize_agent("KAYO") == "KAY/O"
        assert normalize_agent("Kay/o") == "KAY/O"

    def test_case_insensitive_canonical(self):
        assert normalize_agent("jett") == "Jett"
        assert normalize_agent("REYNA") == "Reyna"

    def test_unknown_agent(self):
        # Unknown agents returned as-is
        assert normalize_agent("UnknownAgent") == "UnknownAgent"

    def test_none(self):
        assert normalize_agent(None) is None

    def test_empty(self):
        assert normalize_agent("") is None


# ---------------------------------------------------------------------------
# Economy parser €” buy type from CSS class
# ---------------------------------------------------------------------------


class TestEconomyBuyType:
    def test_eco(self):
        assert MatchEconomyParser._classes_to_buy_type("mod-eco") == "eco"

    def test_semi_eco(self):
        assert MatchEconomyParser._classes_to_buy_type("mod-semi-eco some-other") == "semi-eco"

    def test_semi_buy(self):
        assert MatchEconomyParser._classes_to_buy_type("mod-semi-full highlight") == "semi-buy"

    def test_full_buy(self):
        assert MatchEconomyParser._classes_to_buy_type("mod-full") == "full-buy"

    def test_full_buy_not_confused_with_semi_full(self):
        # mod-semi-full should be semi-buy, NOT full-buy
        result = MatchEconomyParser._classes_to_buy_type("mod-semi-full")
        assert result == "semi-buy"
        assert result != "full-buy"

    def test_unknown(self):
        assert MatchEconomyParser._classes_to_buy_type("mod-unknown") is None

    def test_wl_fraction(self):
        won, played = MatchEconomyParser._parse_wl_fraction("12 / 20")
        assert won == 12
        assert played == 20

    def test_wl_fraction_single(self):
        won, played = MatchEconomyParser._parse_wl_fraction("5")
        assert won == 5
        assert played == 5


# ---------------------------------------------------------------------------
# Overview parser €” basic HTML parsing
# ---------------------------------------------------------------------------

MINIMAL_MATCH_HTML = """
<html><body>
<div class="match-header-vs">
  <div class="match-header-vs-team">
    <a href="/team/1001/sentinels">Sentinels</a>
    <div class="match-header-vs-score">2</div>
  </div>
  <div class="match-header-vs-team">
    <a href="/team/2002/navi">NAVI</a>
    <div class="match-header-vs-score">0</div>
  </div>
</div>
<a class="match-header-event" href="/event/5001/vct-2024">VCT 2024</a>
<div class="match-header-event-series">Playoffs</div>
<div class="match-header-date">
  <div class="moment-tz-convert" data-utc-ts="1712000000">Apr 1, 2024</div>
</div>
<div class="match-header-vs-note">bo3</div>
</body></html>
"""


class TestMatchOverviewParser:
    def test_extracts_match_id(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        assert parser.match_id == 99001

    def test_extracts_team_ids(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        header = parser._parse_match_header()
        assert header["team1_id"] == 1001
        assert header["team2_id"] == 2002

    def test_extracts_scores(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        header = parser._parse_match_header()
        assert header["team1_score"] == 2
        assert header["team2_score"] == 0

    def test_determines_winner(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        header = parser._parse_match_header()
        assert header["winner_team_id"] == 1001

    def test_extracts_event_id(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        header = parser._parse_match_header()
        assert header["event_id"] == 5001

    def test_extracts_best_of(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        header = parser._parse_match_header()
        assert header["best_of"] == 3

    def test_extracts_unix_timestamp(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        header = parser._parse_match_header()
        assert header["unix_timestamp"] == 1712000000

    def test_status_completed_when_scores_present(self):
        parser = MatchOverviewParser(MINIMAL_MATCH_HTML, "https://www.vlr.gg/99001/sen-vs-navi")
        header = parser._parse_match_header()
        assert header["status"] == "completed"


# ---------------------------------------------------------------------------
# Performance parser €” merge logic
# ---------------------------------------------------------------------------


class TestPerformanceParser:
    def test_merge_multikill_and_clutch_rows(self):
        mk_row = {
            "match_id": 1,
            "map_play_id": None,
            "player_id": 42,
            "team_id": 10,
            "kills_2k": 3,
            "kills_3k": 1,
            "kills_4k": 0,
            "kills_5k": 0,
            "kills_2k_rounds": [1, 5, 10],
            "kills_3k_rounds": [7],
            "kills_4k_rounds": [],
            "kills_5k_rounds": [],
        }
        clutch_row = {
            "match_id": 1,
            "map_play_id": None,
            "player_id": 42,
            "team_id": 10,
            "clutches_v1": 2,
            "clutches_v2": 1,
            "clutches_v3": 0,
            "clutches_v4": 0,
            "clutches_v5": 0,
            "clutches_v1_rounds": [8, 15],
            "clutches_v2_rounds": [12],
            "clutches_v3_rounds": [],
            "clutches_v4_rounds": [],
            "clutches_v5_rounds": [],
        }
        merged = MatchPerformanceParser._merge_perf_rows([mk_row, clutch_row])
        assert len(merged) == 1
        m = merged[0]
        assert m["player_id"] == 42
        assert m["kills_2k"] == 3
        assert m["kills_3k"] == 1
        assert m["clutches_v1"] == 2
        assert m["clutches_v2"] == 1
        assert 8 in m["clutches_v1_rounds"]
        assert 15 in m["clutches_v1_rounds"]

    def test_merge_different_players_stay_separate(self):
        row1 = {
            "match_id": 1,
            "map_play_id": None,
            "player_id": 1,
            "team_id": 10,
            "kills_2k": 2,
            "kills_3k": 0,
            "kills_4k": 0,
            "kills_5k": 0,
            "kills_2k_rounds": [3],
            "kills_3k_rounds": [],
            "kills_4k_rounds": [],
            "kills_5k_rounds": [],
        }
        row2 = {
            "match_id": 1,
            "map_play_id": None,
            "player_id": 2,
            "team_id": 10,
            "kills_2k": 1,
            "kills_3k": 0,
            "kills_4k": 0,
            "kills_5k": 0,
            "kills_2k_rounds": [9],
            "kills_3k_rounds": [],
            "kills_4k_rounds": [],
            "kills_5k_rounds": [],
        }
        merged = MatchPerformanceParser._merge_perf_rows([row1, row2])
        assert len(merged) == 2


# ---------------------------------------------------------------------------
# Logs parser €” event type detection
# ---------------------------------------------------------------------------

KILL_EVENT_HTML = """
<div class="vm-stats-game-logs-event mod-kill">
  <a href="/player/100/aspas">aspas</a>
  <img class="weapon" alt="Vandal">
  <a href="/player/200/cNed">cNed</a>
</div>
"""

PLANT_EVENT_HTML = """
<div class="vm-stats-game-logs-event mod-plant">
  <a href="/player/300/yay">yay</a>
</div>
"""


class TestLogsParser:
    def _make_node(self, html: str):
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        return tree.css_first("div")

    def test_detects_kill_event(self):
        parser = MatchLogsParser("", 1)
        node = self._make_node(KILL_EVENT_HTML)
        event_type = parser._determine_event_type(node, node.attributes.get("class", ""))
        assert event_type == "kill"

    def test_detects_plant_event(self):
        parser = MatchLogsParser("", 1)
        node = self._make_node(PLANT_EVENT_HTML)
        event_type = parser._determine_event_type(node, node.attributes.get("class", ""))
        assert event_type == "plant"

    def test_parse_kill_extracts_player_ids(self):
        parser = MatchLogsParser("", 1)
        node = self._make_node(KILL_EVENT_HTML)
        result: dict = {
            "match_id": 1,
            "map_play_id": None,
            "round_number": 1,
            "event_order": 0,
            "event_type": "kill",
            "killer_player_id": None,
            "victim_player_id": None,
            "weapon": None,
            "is_headshot": None,
            "is_wallbang": None,
            "spike_planted_by": None,
            "round_winner_team": None,
            "round_end_reason": None,
        }
        parser._parse_kill_event(node, result)
        assert result["killer_player_id"] == 100
        assert result["victim_player_id"] == 200
        assert result["weapon"] == "Vandal"


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------


class TestTokenBucketRateLimiter:
    def test_acquire_depletes_token(self):
        from vlr_scraper.rate_limiter import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=10.0, capacity=5.0)
        initial = limiter.tokens

        async def run():
            await limiter.acquire(jitter=0)

        asyncio.run(run())
        assert limiter.tokens < initial

    def test_refill_over_time(self):
        import time

        from vlr_scraper.rate_limiter import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=100.0, capacity=10.0)
        limiter.tokens = 0.0
        limiter._last_refill = time.monotonic() - 0.5  # 0.5 seconds ago
        limiter._refill()
        # Should have added ~50 tokens (100 RPS Ã- 0.5s) capped at capacity 10
        assert limiter.tokens == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# DB queries €” in-memory SQLite
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Use a temp SQLite file for DB tests."""
    db_path = str(tmp_path / "test.db")
    import vlr_scraper.connection as conn_mod

    asyncio.run(conn_mod.close_connection())
    monkeypatch.setattr("vlr_scraper.config.settings.DB_PATH", db_path)
    monkeypatch.setattr(conn_mod, "_write_lock", asyncio.Lock())
    monkeypatch.setattr(conn_mod, "_db_lock", asyncio.Lock())

    async def setup():
        from vlr_scraper.connection import init_db

        await init_db()

    asyncio.run(setup())
    yield db_path
    asyncio.run(conn_mod.close_connection())


class TestDBQueries:
    def test_upsert_event(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.upsert_event(
                {
                    "event_id": 1,
                    "name": "VCT 2024",
                    "slug": "vct-2024",
                    "status": "completed",
                    "region": "na",
                }
            )
            ids = await q.get_all_event_ids()
            assert 1 in ids

        asyncio.run(run())

    def test_upsert_team(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.upsert_team(
                {
                    "team_id": 100,
                    "name": "Sentinels",
                    "abbreviation": "SEN",
                    "region": "na",
                }
            )
            from vlr_scraper.connection import execute_read_one

            row = await execute_read_one("SELECT name FROM teams WHERE team_id=?", (100,))
            assert row["name"] == "Sentinels"

        asyncio.run(run())

    def test_upsert_player(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.upsert_player(
                {
                    "player_id": 42,
                    "ign": "aspas",
                    "real_name": "Erick Santos",
                    "country": "Brazil",
                }
            )
            from vlr_scraper.connection import execute_read_one

            row = await execute_read_one("SELECT ign FROM players WHERE player_id=?", (42,))
            assert row["ign"] == "aspas"

        asyncio.run(run())

    def test_queue_add_and_progress(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.queue_add("https://www.vlr.gg/12345/match", "match")
            pending = await q.queue_next_pending("match", limit=10)
            assert len(pending) == 1

            qid = pending[0]["queue_id"]
            await q.queue_mark_in_progress(qid)
            in_prog = await q.queue_next_pending("match", limit=10)
            assert len(in_prog) == 0

            await q.queue_mark_done(qid)
            counts = await q.queue_counts()
            assert counts.get("done", 0) == 1

        asyncio.run(run())

    def test_queue_reset_in_progress(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.queue_add("https://www.vlr.gg/11111/match", "match")
            pending = await q.queue_next_pending("match", limit=10)
            qid = pending[0]["queue_id"]
            await q.queue_mark_in_progress(qid)

            # Simulate crash recovery
            reset = await q.queue_reset_in_progress()
            assert reset == 1

            # Should be pending again
            pending2 = await q.queue_next_pending("match", limit=10)
            assert len(pending2) == 1

        asyncio.run(run())

    def test_queue_retry_uses_cooldown(self, temp_db):
        async def run():
            import vlr_scraper.queries as q
            from vlr_scraper.connection import execute_read_one

            await q.queue_add("https://www.vlr.gg/22222/match", "match")
            pending = await q.queue_next_pending("match", limit=10)
            qid = pending[0]["queue_id"]
            await q.queue_retry_failed(qid, "Bot protection", cooldown_minutes=10)
            ready = await q.queue_next_pending("match", limit=10)
            row = await execute_read_one(
                "SELECT retries, last_error, next_attempt_at FROM crawl_queue WHERE queue_id=?",
                (qid,),
            )
            assert len(ready) == 0
            assert row["retries"] == 1
            assert row["last_error"] == "Bot protection"
            assert row["next_attempt_at"] is not None

        asyncio.run(run())

    def test_queue_claim_pending_is_atomic(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.queue_add("https://www.vlr.gg/33333/match", "match")
            claimed = await q.queue_claim_pending("match", limit=10)
            claimed_again = await q.queue_claim_pending("match", limit=10)
            assert len(claimed) == 1
            assert len(claimed_again) == 0
            assert claimed[0]["status"] == "in_progress"

        asyncio.run(run())

    def test_scrape_run_records_metrics(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            run_id = await q.scrape_run_start("test")
            await q.scrape_run_finish(run_id, "completed", 2, 1, 3, 4)
            runs = await q.scrape_runs_recent(limit=1)
            assert runs[0]["mode"] == "test"
            assert runs[0]["pages_processed"] == 2
            assert runs[0]["cloudflare_blocks"] == 3

        asyncio.run(run())

    def test_upsert_match(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.ensure_teams([1, 2])
            await q.upsert_match(
                {
                    "match_id": 9999,
                    "event_id": None,
                    "team1_id": 1,
                    "team2_id": 2,
                    "team1_score": 2,
                    "team2_score": 1,
                    "winner_team_id": 1,
                    "status": "completed",
                    "url": "https://www.vlr.gg/9999/test",
                }
            )
            from vlr_scraper.connection import execute_read_one

            row = await execute_read_one(
                "SELECT team1_score FROM matches WHERE match_id=?", (9999,)
            )
            assert row["team1_score"] == 2

        asyncio.run(run())

    def test_insert_player_stats_batch(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            # Need a match first
            await q.ensure_team(10)
            await q.ensure_player(1)
            await q.upsert_match(
                {
                    "match_id": 1234,
                    "status": "completed",
                    "url": "https://www.vlr.gg/1234/test",
                }
            )
            await q.insert_player_stats_batch(
                [
                    {
                        "match_id": 1234,
                        "map_play_id": None,
                        "player_id": 1,
                        "team_id": 10,
                        "agent": "Jett",
                        "rating": 1.25,
                        "acs": 287.0,
                        "kills": 22,
                        "deaths": 15,
                        "assists": 3,
                        "kd_diff": 7,
                        "kast": 0.72,
                        "adr": 165.3,
                        "hs_pct": 0.28,
                        "fk": 4,
                        "fd": 2,
                        "fk_diff": 2,
                        "rounds_played": 25,
                    },
                ]
            )
            from vlr_scraper.connection import execute_read_one

            row = await execute_read_one(
                "SELECT kills FROM match_player_stats WHERE match_id=?", (1234,)
            )
            assert row["kills"] == 22

        asyncio.run(run())

    def test_insert_economy_rounds_buy_type(self, temp_db):
        async def run():
            import vlr_scraper.queries as q

            await q.ensure_team(10)
            await q.upsert_match(
                {
                    "match_id": 5555,
                    "status": "completed",
                    "url": "https://www.vlr.gg/5555/test",
                }
            )
            await q.insert_economy_rounds_batch(
                [
                    {
                        "match_id": 5555,
                        "map_play_id": None,
                        "team_id": 10,
                        "round_number": 1,
                        "side": "atk",
                        "buy_type": "eco",
                        "remaining_bank": 800,
                        "loadout_value": 400,
                        "round_won": 0,
                    },
                    {
                        "match_id": 5555,
                        "map_play_id": None,
                        "team_id": 10,
                        "round_number": 2,
                        "side": "atk",
                        "buy_type": "full-buy",
                        "remaining_bank": 1200,
                        "loadout_value": 3800,
                        "round_won": 1,
                    },
                ]
            )
            from vlr_scraper.connection import execute_read

            rows = await execute_read(
                "SELECT buy_type, round_won FROM match_economy_rounds WHERE match_id=?", (5555,)
            )
            buy_types = {r["buy_type"] for r in rows}
            assert "eco" in buy_types
            assert "full-buy" in buy_types

        asyncio.run(run())
