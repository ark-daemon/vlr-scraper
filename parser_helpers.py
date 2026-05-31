"""HTML parsing utilities and data cleaning helpers."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from selectolax.parser import HTMLParser, Node

from config import AGENT_ALIASES, CANONICAL_AGENTS

# ---------------------------------------------------------------------------
# Regex patterns for VLR integer IDs
# ---------------------------------------------------------------------------
RE_PLAYER_ID = re.compile(r"/player/(\d+)/")
RE_TEAM_ID = re.compile(r"/team/(\d+)/")
RE_EVENT_ID = re.compile(r"/event/(\d+)/")
RE_MATCH_ID = re.compile(r"^/(\d+)/")
RE_MATCH_ID2 = re.compile(r"vlr\.gg/(\d+)/")


def extract_player_id(href: str) -> int | None:
    m = RE_PLAYER_ID.search(href or "")
    return int(m.group(1)) if m else None


def extract_team_id(href: str) -> int | None:
    m = RE_TEAM_ID.search(href or "")
    return int(m.group(1)) if m else None


def extract_event_id(href: str) -> int | None:
    m = RE_EVENT_ID.search(href or "")
    return int(m.group(1)) if m else None


def extract_match_id(href: str) -> int | None:
    m = RE_MATCH_ID.match(href or "") or RE_MATCH_ID2.search(href or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    return cleaned if cleaned else None


def parse_int(value: str | None, default: int | None = None) -> int | None:
    if value is None:
        return default
    cleaned = re.sub(r"[^\d\-]", "", str(value).strip())
    try:
        return int(cleaned)
    except ValueError:
        return default


def parse_float(value: str | None, default: float | None = None) -> float | None:
    if value is None:
        return default
    text = str(value).strip()
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not m:
        return default
    token = m.group(0)
    if "," in token and "." not in token:
        left, right = token.split(",", 1)
        # Treat 1,234 as thousands, 1,23 as decimal.
        if len(right) == 3 and len(left) >= 1:
            cleaned = f"{left}{right}"
        else:
            cleaned = f"{left}.{right}"
    elif "," in token and "." in token:
        # 1,234.56 -> 1234.56
        cleaned = token.replace(",", "")
    else:
        cleaned = token
    try:
        return float(cleaned)
    except ValueError:
        return default


def parse_percent(value: str | None) -> float | None:
    """Parse '72%' â†’ 0.72"""
    if value is None:
        return None
    cleaned = str(value).strip().rstrip("%")
    try:
        return float(cleaned) / 100.0
    except ValueError:
        return None


def parse_bank(value: str | None) -> int | None:
    """Parse '$3,800' â†’ 3800"""
    if value is None:
        return None
    cleaned = re.sub(r"[^\d]", "", str(value))
    return int(cleaned) if cleaned else None


def parse_kd_diff(value: str | None) -> int | None:
    """Parse '+12' or '-5' â†’ int"""
    if value is None:
        return None
    cleaned = str(value).strip().replace("+", "")
    try:
        return int(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Agent normalization
# ---------------------------------------------------------------------------


def normalize_agent(name: str | None) -> str | None:
    if not name:
        return None
    name = name.strip()
    # Try alias map first
    canonical = AGENT_ALIASES.get(name)
    if canonical:
        return canonical
    # Case-insensitive lookup in canonical list
    lower = name.lower()
    for agent in CANONICAL_AGENTS:
        if agent.lower() == lower:
            return agent
    return name  # Return as-is if unknown


# ---------------------------------------------------------------------------
# selectolax helpers
# ---------------------------------------------------------------------------


def sel_text(node: Node | None) -> str | None:
    if node is None:
        return None
    return clean_text(node.text(strip=True))


def sel_attr(node: Node | None, attr: str) -> str | None:
    if node is None:
        return None
    return node.attributes.get(attr)


def sel_css(root: HTMLParser | Node, selector: str) -> Node | None:
    result = root.css_first(selector)
    return result


def sel_css_all(root: HTMLParser | Node, selector: str) -> list[Node]:
    return root.css(selector)


def parse_selectolax(html: str) -> HTMLParser:
    return HTMLParser(html)


# ---------------------------------------------------------------------------
# BeautifulSoup helpers (fallback)
# ---------------------------------------------------------------------------


def parse_bs4(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def bs4_text(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    return clean_text(tag.get_text())


def bs4_attr(tag: Tag | None, attr: str) -> str | None:
    if tag is None:
        return None
    val = tag.get(attr)
    if isinstance(val, list):
        return " ".join(val)
    return str(val) if val else None


# ---------------------------------------------------------------------------
# Cloudflare detection
# ---------------------------------------------------------------------------


def is_cloudflare_challenge(html: str) -> bool:
    return "<title>Just a moment</title>" in html or "cf-browser-verification" in html


def is_404(html: str) -> bool:
    return "404" in html and ("not found" in html.lower() or "page not found" in html.lower())


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

BASE_URL = "https://www.vlr.gg"


def full_url(path: str) -> str:
    if path.startswith("http"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return BASE_URL + path


# ---------------------------------------------------------------------------
# Table row parser for Overview stats
# ---------------------------------------------------------------------------


def parse_overview_row(
    tds: list[Node | Tag],
    match_id: int,
    map_play_id: int | None,
    team_id: int | None,
) -> dict[str, Any] | None:
    """
    Parse a single player row from the Overview stats table.
    Column order: [0]=Player [1]=Agent [2]=Rating [3]=ACS [4]=K [5]=D [6]=A
    [7]=+/- [8]=KAST% [9]=ADR [10]=HS% [11]=FK [12]=FD [13]=FKÂ±
    """
    if len(tds) < 14:
        return None

    # Determine if these are selectolax Nodes or BS4 Tags
    if isinstance(tds[0], Node):

        def get_text(td: Node) -> str:
            return clean_text(td.text(strip=True)) or ""

        def get_attr(td: Node, a: str) -> str | None:
            return td.attributes.get(a)

        def find_child(td: Node, sel: str) -> Node | None:
            return td.css_first(sel)

        def find_attr_in(td: Node, child_sel: str, attr: str) -> str | None:
            child = td.css_first(child_sel)
            return child.attributes.get(attr) if child else None
    else:

        def get_text(td):
            return bs4_text(td) or ""

        def get_attr(td, a):
            return td.get(a)

        def find_child(td, sel):
            return td.select_one(sel)

        def find_attr_in(td, child_sel, attr):
            child = td.select_one(child_sel)
            return child.get(attr) if child else None

    # Player ID from link href
    player_href = find_attr_in(tds[0], "a", "href") or ""
    player_id = extract_player_id(player_href)
    if not player_id:
        return None

    # Agent from img alt
    agent_raw = find_attr_in(tds[1], "img", "alt")
    agent = normalize_agent(agent_raw)

    def col(i: int) -> str:
        return get_text(tds[i])

    return {
        "match_id": match_id,
        "map_play_id": map_play_id,
        "player_id": player_id,
        "team_id": team_id,
        "agent": agent,
        "rating": parse_float(col(2)),
        "acs": parse_float(col(3)),
        "kills": parse_int(col(4)),
        "deaths": parse_int(col(5)),
        "assists": parse_int(col(6)),
        "kd_diff": parse_kd_diff(col(7)),
        "kast": parse_percent(col(8)),
        "adr": parse_float(col(9)),
        "hs_pct": parse_percent(col(10)),
        "fk": parse_int(col(11)),
        "fd": parse_int(col(12)),
        "fk_diff": parse_kd_diff(col(13)),
        "rounds_played": None,  # derived from map, set by caller if known
    }
