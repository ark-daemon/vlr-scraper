"""Base async scraper with HTTP client, rate limiting, retry logic, and circuit breaker."""

from __future__ import annotations

import asyncio
import json
import random
import time
import traceback
from pathlib import Path

import cloakbrowser
import httpx
from loguru import logger

from vlr_scraper.config import settings
from vlr_scraper.parser_helpers import is_404, is_cloudflare_challenge
from vlr_scraper.rate_limiter import get_limiter

# ---------------------------------------------------------------------------
# Circuit breaker €” shared across all scraper instances
# ---------------------------------------------------------------------------
_consecutive_failures: int = 0
_cb_lock = asyncio.Lock()
CIRCUIT_BREAKER_THRESHOLD: int = 3  # consecutive failures before tripping
CIRCUIT_BREAKER_COOLDOWN: int = 180  # seconds to pause when tripped
CIRCUIT_BREAKER_ESCALATION: float = 1.5  # cooldown multiplier per consecutive trip
_cb_trips: int = 0  # how many times breaker has tripped
_browser_cookies: list[dict] = []
_browser_user_agent: str | None = None
_browser_last_refresh_at: float = 0.0
_browser_lock = asyncio.Lock()
_cloudflare_blocks: int = 0
_cloakbrowser_successes: int = 0


class ScraperError(Exception):
    pass


class CloudflareBlockError(ScraperError):
    pass


class NotFoundError(ScraperError):
    pass


class AsyncScraper:
    """
    Base class for all VLR scrapers.
    Provides rate-limited, retry-aware HTTP GET with connection pooling.
    """

    def __init__(self) -> None:
        self.limiter = get_limiter(
            rate=settings.RATE_LIMIT_RPS,
            capacity=float(settings.CONCURRENCY),
        )
        self._client: httpx.AsyncClient | None = None
        self._errors_log = Path(settings.ERRORS_LOG)

    async def _get_client(self) -> httpx.AsyncClient:
        self._load_browser_session()
        if self._client is None or self._client.is_closed:
            headers = {"User-Agent": _browser_user_agent or settings.USER_AGENT}
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(settings.REQUEST_TIMEOUT),
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                ),
            )
            for cookie in _browser_cookies:
                name = cookie.get("name")
                value = cookie.get("value")
                if name and value:
                    self._client.cookies.set(
                        name,
                        value,
                        domain=cookie.get("domain") or ".vlr.gg",
                        path=cookie.get("path") or "/",
                    )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncScraper:
        await self._get_client()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def get(self, url: str) -> str:
        """
        Fetch a URL with rate limiting, exponential backoff retries,
        and a global circuit breaker that pauses when VLR starts throttling.
        Returns HTML string or raises ScraperError.
        """
        # Check circuit breaker before even starting
        await self._check_circuit_breaker()

        client = await self._get_client()
        last_exc: Exception | None = None

        for attempt in range(settings.RETRY_MAX):
            # Re-check circuit breaker before each retry (not just first attempt)
            if attempt > 0:
                await self._check_circuit_breaker()

            await self.limiter.acquire()
            try:
                response = await client.get(url)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    await self.limiter.wait_for_retry_after(retry_after)
                    continue

                if response.status_code == 404:
                    raise NotFoundError(f"404 Not Found: {url}")

                html = response.text

                if is_cloudflare_challenge(html) or response.status_code == 403:
                    self._record_cloudflare_block()
                    logger.warning(
                        f"Cloudflare challenge detected for {url}. Fetching with CloakBrowser..."
                    )
                    last_exc = CloudflareBlockError(f"Bot protection detected for {url}")
                    browser_html = await self._fetch_with_cloakbrowser(url)
                    # Force client recreation with new headers/cookies
                    if self._client and not self._client.is_closed:
                        await self._client.aclose()
                        self._client = None
                    client = await self._get_client()
                    if browser_html:
                        if is_404(browser_html):
                            raise NotFoundError(f"404 Not Found: {url}")
                        await self._record_success()
                        return browser_html
                    continue

                if response.status_code >= 500:
                    raise ScraperError(f"Server error {response.status_code} for {url}")

                response.raise_for_status()

                # Success - reset circuit breaker
                await self._record_success()
                return html

            except NotFoundError:
                raise
            except CloudflareBlockError as exc:
                last_exc = exc
                await self._record_failure()
                logger.warning(f"CloakBrowser fetch failed on attempt {attempt + 1}: {exc}")
            except httpx.TimeoutException as exc:
                last_exc = exc
                await self._record_failure()  # count EACH timeout for breaker
                logger.warning(f"Timeout on attempt {attempt + 1} for {url}: {exc}")
            except httpx.RequestError as exc:
                last_exc = exc
                await self._record_failure()  # count EACH error for breaker
                logger.warning(f"Request error on attempt {attempt + 1} for {url}: {exc}")
            except ScraperError as exc:
                last_exc = exc
                logger.warning(f"Scraper error on attempt {attempt + 1} for {url}: {exc}")

            if attempt < settings.RETRY_MAX - 1:
                backoff = settings.RETRY_BACKOFF_BASE**attempt + random.uniform(0, 1)
                logger.debug(f"Backing off {backoff:.1f}s before retry {attempt + 2}")
                await asyncio.sleep(backoff)

        if isinstance(last_exc, CloudflareBlockError):
            raise last_exc
        raise ScraperError(f"All {settings.RETRY_MAX} attempts failed for {url}") from last_exc

    @staticmethod
    async def _check_circuit_breaker() -> None:
        """If consecutive failures exceed threshold, pause to let VLR recover."""
        global _consecutive_failures, _cb_trips
        cooldown = 0
        async with _cb_lock:
            if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                cooldown = int(CIRCUIT_BREAKER_COOLDOWN * (CIRCUIT_BREAKER_ESCALATION**_cb_trips))
                _cb_trips += 1
                _consecutive_failures = 0  # reset so only one pause per burst
                logger.warning(
                    f"Circuit breaker tripped (trip #{_cb_trips}). "
                    f"Pausing {cooldown}s to let VLR rate limit reset."
                )
        # Sleep outside lock so other coroutines can proceed or also wait
        if cooldown > 0:
            await asyncio.sleep(cooldown)

    @staticmethod
    async def _record_success() -> None:
        global _consecutive_failures, _cb_trips
        async with _cb_lock:
            _consecutive_failures = 0
            _cb_trips = max(0, _cb_trips - 1)  # de-escalate

    @staticmethod
    async def _record_failure() -> None:
        global _consecutive_failures
        async with _cb_lock:
            _consecutive_failures += 1

    def _log_error(self, url: str, error: str, tb: str = "", html_snippet: str = "") -> None:
        entry = (
            f"URL: {url}\nERROR: {error}\nTRACEBACK: {tb}\nHTML: {html_snippet[:500]}\n{'=' * 60}\n"
        )
        try:
            self._errors_log.parent.mkdir(parents=True, exist_ok=True)
            with self._errors_log.open("a", encoding="utf-8") as f:
                f.write(entry)
        except OSError:
            pass
        logger.error(f"Parser error for {url}: {error}")

    async def _fetch_with_cloakbrowser(self, url: str) -> str | None:
        global _browser_cookies, _browser_user_agent, _browser_last_refresh_at
        async with _browser_lock:
            if _browser_cookies and time.time() - _browser_last_refresh_at < 60:
                wait = 60.0 - (time.time() - _browser_last_refresh_at) + 1.0
                logger.info(
                    f"CloakBrowser session fresh. Waiting {wait:.0f}s before next launch."
                )
                await asyncio.sleep(wait)

            browser = None
            logger.info("Launching CloakBrowser for challenged page...")
            try:
                browser = await cloakbrowser.launch_async(
                    headless=settings.CLOAKBROWSER_HEADLESS,
                    humanize=settings.CLOAKBROWSER_HUMANIZE,
                )
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=settings.REQUEST_TIMEOUT * 1000,
                )

                logger.info("Waiting for challenged page to become readable...")
                attempts = max(1, settings.CLOAKBROWSER_WAIT_SECONDS // 2)
                for _ in range(attempts):
                    html = await page.content()
                    cookies = await context.cookies()
                    if not is_cloudflare_challenge(html):
                        _browser_cookies = cookies
                        _browser_user_agent = await page.evaluate("navigator.userAgent")
                        _browser_last_refresh_at = time.time()
                        self._record_cloakbrowser_success()
                        self._save_browser_session()
                        logger.success("CloakBrowser fetched challenged page successfully.")
                        return html
                    await asyncio.sleep(2)

                raise CloudflareBlockError(
                    f"CloakBrowser page was still challenged after {settings.CLOAKBROWSER_WAIT_SECONDS} seconds."
                )
            except CloudflareBlockError:
                raise
            except Exception as exc:
                logger.error(f"CloakBrowser error during challenged fetch: {exc}")
                raise CloudflareBlockError(f"CloakBrowser fetch failed: {exc}") from exc
            finally:
                if browser is not None:
                    await browser.close()

    @staticmethod
    def _load_browser_session() -> None:
        global _browser_cookies, _browser_user_agent, _browser_last_refresh_at
        if _browser_cookies:
            return
        session_path = Path(settings.CLOAKBROWSER_SESSION_PATH)
        if not session_path.exists():
            return
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
            cookies = data.get("cookies") or []
            user_agent = data.get("user_agent")
            if isinstance(cookies, list):
                _browser_cookies = cookies
            if isinstance(user_agent, str) and user_agent:
                _browser_user_agent = user_agent
            _browser_last_refresh_at = float(data.get("saved_at") or 0.0)
            logger.info(f"Loaded CloakBrowser session from {session_path}")
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"Could not load CloakBrowser session: {exc}")

    @staticmethod
    def _save_browser_session() -> None:
        session_path = Path(settings.CLOAKBROWSER_SESSION_PATH)
        payload = {
            "cookies": _browser_cookies,
            "user_agent": _browser_user_agent,
            "saved_at": _browser_last_refresh_at,
        }
        try:
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Could not save CloakBrowser session: {exc}")

    @staticmethod
    def _record_cloudflare_block() -> None:
        global _cloudflare_blocks
        _cloudflare_blocks += 1

    @staticmethod
    def _record_cloakbrowser_success() -> None:
        global _cloakbrowser_successes
        _cloakbrowser_successes += 1

    @staticmethod
    def get_runtime_metrics(reset: bool = False) -> dict[str, int]:
        global _cloudflare_blocks, _cloakbrowser_successes
        metrics = {
            "cloudflare_blocks": _cloudflare_blocks,
            "cloakbrowser_successes": _cloakbrowser_successes,
        }
        if reset:
            _cloudflare_blocks = 0
            _cloakbrowser_successes = 0
        return metrics

    async def safe_scrape(self, url: str, parser_fn, *args, **kwargs):
        """
        Call parser_fn(html, *args, **kwargs) safely.
        One broken page must not crash the entire crawl.
        """
        try:
            html = await self.get(url)
            return await parser_fn(html, *args, **kwargs)
        except (NotFoundError, CloudflareBlockError) as exc:
            logger.warning(str(exc))
            raise
        except ScraperError as exc:
            self._log_error(url, str(exc), traceback.format_exc())
            raise
        except Exception as exc:
            self._log_error(url, str(exc), traceback.format_exc())
            raise ScraperError(str(exc)) from exc
