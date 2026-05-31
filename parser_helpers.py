"""HTML parsing utilities and data cleaning helpers."""

from __future__ import annotations

import re

from config import AGENT_ALIASES, CANONICAL_AGENTS, settings

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
# (intentionally empty — direct selectolax API used throughout)


# ---------------------------------------------------------------------------
# BeautifulSoup helpers (fallback)
# ---------------------------------------------------------------------------
# (removed — no longer used in the codebase)


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

def full_url(path: str) -> str:
    if path.startswith("http"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return settings.BASE_URL + path


# (parse_overview_row removed — dead code; overview parsing is done inline in match_overview.py)
