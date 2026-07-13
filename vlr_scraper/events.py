"""Events scraper €” crawls all VLR regions and seeds the crawl queue."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger
from selectolax.parser import HTMLParser

import vlr_scraper.queries as queries
from vlr_scraper.base import AsyncScraper, CloudflareBlockError, NotFoundError, ScraperError
from vlr_scraper.config import VLR_REGIONS, settings
from vlr_scraper.parser_helpers import (
    clean_text,
    extract_event_id,
    extract_match_id,
    extract_team_id,
    full_url,
)

BASE_URL = settings.BASE_URL
RE_STRICT_MATCH_PATH = re.compile(r"^/\d+/[^/?#]+/?$")


class EventsScraper(AsyncScraper):
    async def seed_all_regions(self) -> None:
        """Seed the crawl queue from all regions and all event statuses."""
        for region in ["all"] + VLR_REGIONS:
            await self.scrape_events_list(region)

    async def scrape_events_list(self, region: str = "all") -> list[dict[str, Any]]:
        """Scrape the events listing page for a region and enqueue all events."""
        url = f"{BASE_URL}/events/{region}" if region != "all" else f"{BASE_URL}/events"
        logger.info(f"Scraping events list: {url}")

        try:
            html = await self.get(url)
        except ScraperError as exc:
            logger.error(f"Failed to fetch events list {url}: {exc}")
            return []

        events = self._parse_events_list(html, region)

        # Enqueue event pages and their match/team links
        queue_entries: list[tuple[str, str]] = []
        for ev in events:
            if ev.get("url"):
                queue_entries.append((ev["url"], "event"))
            await queries.upsert_event(ev)

        if queue_entries:
            await queries.queue_add_many(queue_entries)

        logger.info(f"Found {len(events)} events for region={region}")
        return events

    def _parse_events_list(self, html: str, region: str) -> list[dict[str, Any]]:
        tree = HTMLParser(html)
        events: list[dict[str, Any]] = []

        # VLR groups events into sections: live, upcoming, completed
        for section in tree.css("div.events-container-col"):
            # Determine status from section header
            header_node = section.css_first("div.events-container-col-header")
            header_text = clean_text(header_node.text(strip=True)) if header_node else ""
            if "live" in header_text.lower():
                status = "ongoing"
            elif "upcoming" in header_text.lower():
                status = "upcoming"
            else:
                status = "completed"

            for card in section.css("a.event-item"):
                href = card.attributes.get("href") or ""
                event_id = extract_event_id(href)
                if not event_id:
                    continue

                name_node = card.css_first("div.event-item-title")
                name = clean_text(name_node.text(strip=True)) if name_node else None

                # Dates
                date_node = card.css_first("div.event-item-desc-item-value")
                dates_text = clean_text(date_node.text(strip=True)) if date_node else ""
                start_date, end_date = self._parse_date_range(dates_text)

                # Prize pool, tier, region from desc items
                desc_items = card.css("div.event-item-desc-item")
                prize_pool = None
                tier = None
                ev_region = region if region != "all" else None

                for item in desc_items:
                    label_node = item.css_first("div.event-item-desc-item-label")
                    val_node = item.css_first("div.event-item-desc-item-value")
                    label = clean_text(label_node.text(strip=True)) if label_node else ""
                    val = clean_text(val_node.text(strip=True)) if val_node else ""
                    if label and "prize" in label.lower():
                        prize_pool = val
                    elif label and "tier" in label.lower():
                        tier = val
                    elif label and "region" in label.lower():
                        ev_region = val

                # Logo
                logo_img = card.css_first("img.event-item-logo")
                logo_url = logo_img.attributes.get("src") if logo_img else None

                slug = href.strip("/").split("/")[-1] if href else None

                events.append(
                    {
                        "event_id": event_id,
                        "name": name or f"Event {event_id}",
                        "slug": slug,
                        "status": status,
                        "region": ev_region,
                        "tier": tier,
                        "prize_pool": prize_pool,
                        "start_date": start_date,
                        "end_date": end_date,
                        "logo_url": logo_url,
                        "url": full_url(href),
                    }
                )

        return events

    async def scrape_event_url(self, url: str, queue_id: int | None = None) -> bool:
        """Scrape a queued event URL and mark queue status."""
        event_id = extract_event_id(url)
        if not event_id:
            logger.warning(f"Could not extract event_id from: {url}")
            if queue_id is not None:
                await queries.queue_mark_done(queue_id)
            return False

        slug = ""
        parts = url.split("/")
        if "event" in parts:
            idx = parts.index("event")
            if len(parts) > idx + 2:
                slug = parts[idx + 2].split("?")[0]

        try:
            ok = await self.scrape_event_page(event_id, slug)
        except CloudflareBlockError as exc:
            logger.warning(f"Cloudflare block: {url}: {exc}")
            if queue_id is not None:
                await queries.queue_retry_failed(queue_id, str(exc))
            return False
        if queue_id is not None:
            if ok:
                await queries.queue_mark_done(queue_id)
            else:
                await queries.queue_mark_failed(queue_id, "Failed to scrape event page")
        return ok

    async def scrape_event_page(self, event_id: int, slug: str = "") -> bool:
        """Scrape a specific event page and seed match + team URLs into queue."""
        url = f"{BASE_URL}/event/{event_id}/{slug}"
        logger.info(f"Scraping event page: {url}")

        try:
            html = await self.get(url)
        except CloudflareBlockError as exc:
            logger.warning(f"Cloudflare block for event page {url}: {exc}")
            raise
        except (NotFoundError, ScraperError) as exc:
            logger.error(f"Failed to fetch event page {url}: {exc}")
            return False

        await self._parse_event_page(html, event_id, slug, url)

        # Also scrape the matches page
        matches_url = f"{url}/?series_id=all"
        try:
            matches_html = await self.get(matches_url)
            await self._parse_event_matches_page(matches_html, event_id)
        except CloudflareBlockError as exc:
            logger.warning(f"Cloudflare block for event matches page {matches_url}: {exc}")
            raise
        except ScraperError as exc:
            logger.warning(f"Could not fetch event matches page: {exc}")
        return True

    async def _parse_event_page(self, html: str, event_id: int, slug: str, url: str) -> None:
        tree = HTMLParser(html)

        # Update event metadata
        name_node = tree.css_first("h1.wf-title")
        name = clean_text(name_node.text(strip=True)) if name_node else f"Event {event_id}"

        # Dates
        dates_node = tree.css_first("div.event-header-desc-item-value")
        dates_text = clean_text(dates_node.text(strip=True)) if dates_node else ""
        start_date, end_date = self._parse_date_range(dates_text)

        # Prize pool from header items
        prize_pool = None
        tier = None
        region = None
        for item in tree.css("div.event-header-desc-item"):
            label_node = item.css_first("div.event-header-desc-item-label")
            val_node = item.css_first("div.event-header-desc-item-value")
            label = clean_text(label_node.text(strip=True)) if label_node else ""
            val = clean_text(val_node.text(strip=True)) if val_node else ""
            if label and "prize" in label.lower():
                prize_pool = val
            elif label and "tier" in label.lower():
                tier = val
            elif label and ("region" in label.lower() or "series" in label.lower()):
                region = val

        logo_img = tree.css_first("img.event-header-thumb")
        logo_url = logo_img.attributes.get("src") if logo_img else None

        await queries.upsert_event(
            {
                "event_id": event_id,
                "name": name,
                "slug": slug,
                "region": region,
                "tier": tier,
                "prize_pool": prize_pool,
                "start_date": start_date,
                "end_date": end_date,
                "logo_url": logo_url,
                "url": url,
            }
        )

        # Collect team URLs
        team_entries: list[tuple[str, str]] = []
        for a_tag in tree.css("a[href*='/team/']"):
            href = a_tag.attributes.get("href", "")
            team_id = extract_team_id(href)
            if team_id:
                team_entries.append((full_url(href.split("?")[0]), "team"))

        if team_entries:
            await queries.queue_add_many(list(set(team_entries)))

    async def _parse_event_matches_page(self, html: str, event_id: int) -> int:
        """Extract all match URLs from an event's matches page."""
        tree = HTMLParser(html)
        match_entries: list[tuple[str, str]] = []

        for a_tag in tree.css("a[href]"):
            href = a_tag.attributes.get("href") or ""
            if not RE_STRICT_MATCH_PATH.match(href):
                continue
            match_id = extract_match_id(href)
            if match_id:
                match_entries.append((full_url(href.split("?")[0]), "match"))

        # Deduplicate
        unique = list(set(match_entries))
        if unique:
            await queries.queue_add_many(unique)
        logger.debug(f"Event {event_id}: enqueued {len(unique)} match URLs")
        return len(unique)

    @staticmethod
    def _parse_date_range(text: str) -> tuple[str | None, str | None]:
        """Parse 'Jan 01 – Jan 31, 2025' into start/end date strings."""
        if not text:
            return None, None
        # Try to split on em-dash or regular dash
        parts = re.split(r"[\u2013\-\u2014]", text, maxsplit=1)
        if len(parts) == 2:
            return clean_text(parts[0]), clean_text(parts[1])
        return clean_text(text), None
