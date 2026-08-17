"""Async Playwright crawler that finds broken links, broken images and missing alt tags."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Iterable, Optional
from urllib.parse import urldefrag, urlparse, urlunparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from sqlmodel import Session, select

from .database import engine
from .models import (
    AltStatus,
    IssueType,
    ResourceType,
    Scan,
    ScanItem,
    ScanStatus,
    utcnow,
)

logger = logging.getLogger("linkspector.crawler")

SKIPPED_SCHEMES = {"mailto", "tel", "javascript", "data", "blob", "about", "sms", "ftp"}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Linkspector/0.1"
)

# Servers that reject HEAD outright; retry those with a ranged GET.
HEAD_UNSUPPORTED = {400, 401, 403, 405, 406, 409, 429, 501}


@dataclass
class CrawlConfig:
    max_depth: int = 2
    max_pages: int = 50
    page_timeout_ms: int = 25_000
    request_timeout_ms: int = 15_000
    page_concurrency: int = 3
    check_concurrency: int = 10
    ignore_https_errors: bool = True


@dataclass
class UrlCheck:
    status: Optional[int]
    ok: bool
    content_type: str = ""
    final_url: str = ""
    detail: Optional[str] = None


EXTRACT_ASSETS_JS = """
() => {
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const raw = a.getAttribute('href');
    if (!raw) continue;
    links.push(a.href || raw);
  }
  const images = [];
  for (const img of document.querySelectorAll('img')) {
    const raw = img.getAttribute('src');
    const src = img.src || raw || '';
    images.push({
      src,
      hasAlt: img.hasAttribute('alt'),
      alt: img.getAttribute('alt') || '',
    });
  }
  return { links, images };
}
"""


def normalize_url(url: str) -> Optional[str]:
    """Drop fragments and unsupported schemes; return a comparable absolute URL."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None

    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme in SKIPPED_SCHEMES:
        return None

    url, _ = urldefrag(url)
    if not url:
        return None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None

    return urlunparse(parsed._replace(fragment=""))


def registrable_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_same_site(url: str, root_url: str) -> bool:
    return registrable_host(url) == registrable_host(root_url)


def describe_failure(result: "UrlCheck") -> str:
    if result.detail:
        return result.detail
    if result.status is None:
        return "Unreachable"
    try:
        return f"HTTP {result.status} {HTTPStatus(result.status).phrase}".strip()
    except ValueError:
        return f"HTTP {result.status}"


def short_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    message = text[0] if text else exc.__class__.__name__
    # Playwright prefixes messages with the call that raised them, e.g.
    # "APIRequestContext.head: connect ECONNREFUSED 127.0.0.1:9".
    if ": " in message and message.split(": ", 1)[0].replace(".", "").isalnum():
        message = message.split(": ", 1)[1]
    if "Timeout" in message and "exceeded" in message:
        return "Request timed out"
    return message[:300]


async def _skip_heavy_resources(route: Any) -> None:
    try:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()
    except PlaywrightError:
        pass


class ScanRecorder:
    """Buffers issue rows and progress counters into SQLite."""

    def __init__(self, scan_id: int) -> None:
        self.scan_id = scan_id
        self.pages_checked = 0
        self.links_checked = 0
        self.images_checked = 0
        self.broken_links = 0
        self.broken_images = 0
        self.missing_alt_tags = 0
        self._pending: list[ScanItem] = []
        self._lock = asyncio.Lock()

    def add_issue(
        self,
        *,
        source_url: str,
        target_url: str,
        resource_type: ResourceType,
        issue_type: IssueType,
        status_code: Optional[int] = None,
        alt_status: Optional[AltStatus] = None,
        detail: Optional[str] = None,
    ) -> None:
        if issue_type is IssueType.broken_link:
            self.broken_links += 1
        elif issue_type is IssueType.broken_image:
            self.broken_images += 1
        elif issue_type is IssueType.missing_alt:
            self.missing_alt_tags += 1

        self._pending.append(
            ScanItem(
                scan_id=self.scan_id,
                source_url=source_url[:2000],
                target_url=target_url[:2000],
                resource_type=resource_type,
                issue_type=issue_type,
                status_code=status_code,
                alt_status=alt_status,
                detail=detail,
            )
        )

    def discard_pending(self) -> None:
        self._pending.clear()

    async def flush(
        self,
        *,
        current_url: Optional[str] = None,
        status: Optional[ScanStatus] = None,
        error: Optional[str] = None,
    ) -> None:
        async with self._lock:
            pending, self._pending = self._pending, []
            await asyncio.to_thread(self._write, pending, current_url, status, error)

    def _write(
        self,
        pending: list[ScanItem],
        current_url: Optional[str],
        status: Optional[ScanStatus],
        error: Optional[str],
    ) -> None:
        with Session(engine) as session:
            for item in pending:
                session.add(item)

            scan = session.get(Scan, self.scan_id)
            if scan is not None:
                scan.pages_checked = self.pages_checked
                scan.links_checked = self.links_checked
                scan.images_checked = self.images_checked
                scan.broken_links = self.broken_links
                scan.broken_images = self.broken_images
                scan.missing_alt_tags = self.missing_alt_tags
                scan.current_url = current_url
                if error is not None:
                    scan.error = error
                if status is not None:
                    scan.status = status
                    if status is ScanStatus.running and scan.started_at is None:
                        scan.started_at = utcnow()
                    if status in (ScanStatus.completed, ScanStatus.failed):
                        scan.finished_at = utcnow()
                session.add(scan)

            session.commit()


class SiteCrawler:
    def __init__(self, scan_id: int, start_url: str, config: CrawlConfig) -> None:
        self.scan_id = scan_id
        self.start_url = start_url
        self.config = config
        self.recorder = ScanRecorder(scan_id)

        self.root_failure: Optional[str] = None
        self.visited_pages: set[str] = set()
        self.url_cache: dict[str, UrlCheck] = {}
        self.reported: set[tuple[str, str, str]] = set()
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._check_semaphore = asyncio.Semaphore(config.check_concurrency)

    # ---------------------------------------------------------------- helpers

    def _dedupe(self, source: str, target: str, issue: IssueType, extra: str = "") -> bool:
        key = (source, target, f"{issue.value}:{extra}")
        if key in self.reported:
            return False
        self.reported.add(key)
        return True

    async def check_url(self, api_request: Any, url: str) -> UrlCheck:
        cached = self.url_cache.get(url)
        if cached is not None:
            return cached

        lock = self._cache_locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self.url_cache.get(url)
            if cached is not None:
                return cached

            async with self._check_semaphore:
                result = await self._perform_check(api_request, url)

            self.url_cache[url] = result
            return result

    async def _perform_check(self, api_request: Any, url: str) -> UrlCheck:
        try:
            response = await api_request.head(
                url,
                timeout=self.config.request_timeout_ms,
                max_redirects=10,
                fail_on_status_code=False,
            )
            if response.status in HEAD_UNSUPPORTED:
                response = await api_request.get(
                    url,
                    timeout=self.config.request_timeout_ms,
                    max_redirects=10,
                    fail_on_status_code=False,
                    headers={"Range": "bytes=0-2047"},
                )
        except PlaywrightError as exc:
            message = short_error(exc)
            if "Max redirect count exceeded" in message:
                message = "Broken redirect: too many redirects"
            return UrlCheck(status=None, ok=False, detail=message)
        except Exception as exc:  # noqa: BLE001 - transport failures vary wildly
            return UrlCheck(status=None, ok=False, detail=short_error(exc))

        headers = response.headers or {}
        final_url = response.url or url
        detail = None
        if normalize_url(final_url) != normalize_url(url):
            detail = f"Redirected to {final_url}"
            if response.status >= 400:
                detail = f"Broken redirect -> {final_url}"

        return UrlCheck(
            status=response.status,
            ok=response.status < 400,
            content_type=headers.get("content-type", ""),
            final_url=final_url,
            detail=detail,
        )

    # ------------------------------------------------------------------ crawl

    async def run(self) -> None:
        await self.recorder.flush(status=ScanStatus.running, current_url=self.start_url)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            api_request = await playwright.request.new_context(
                user_agent=USER_AGENT,
                ignore_https_errors=self.config.ignore_https_errors,
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                ignore_https_errors=self.config.ignore_https_errors,
            )
            # Images/fonts/media are validated over HTTP separately, so skip fetching them.
            await context.route("**/*", _skip_heavy_resources)

            try:
                await self._crawl(context, api_request)
            finally:
                await api_request.dispose()
                await context.close()
                await browser.close()

        # A start page that never loaded means there was nothing to audit.
        if self.root_failure is not None:
            await self.recorder.flush(
                status=ScanStatus.failed, current_url=None, error=self.root_failure
            )
        else:
            await self.recorder.flush(status=ScanStatus.completed, current_url=None)

    async def _crawl(self, context: Any, api_request: Any) -> None:
        frontier = [self.start_url]
        depth = 0

        while frontier and depth <= self.config.max_depth:
            remaining = self.config.max_pages - len(self.visited_pages)
            if remaining <= 0:
                break

            batch: list[str] = []
            for url in frontier:
                if url in self.visited_pages or url in batch:
                    continue
                batch.append(url)
                if len(batch) >= remaining:
                    break

            if not batch:
                break

            self.visited_pages.update(batch)
            semaphore = asyncio.Semaphore(self.config.page_concurrency)
            results = await asyncio.gather(
                *(self._guarded_page(semaphore, context, api_request, url, depth) for url in batch)
            )

            next_frontier: list[str] = []
            seen: set[str] = set()
            for discovered in results:
                for url in discovered:
                    if url not in self.visited_pages and url not in seen:
                        seen.add(url)
                        next_frontier.append(url)

            frontier = next_frontier
            depth += 1

    async def _guarded_page(
        self,
        semaphore: asyncio.Semaphore,
        context: Any,
        api_request: Any,
        url: str,
        depth: int,
    ) -> list[str]:
        async with semaphore:
            try:
                discovered = await self._process_page(context, api_request, url, depth)
            except Exception as exc:  # noqa: BLE001 - one bad page must not kill the scan
                logger.warning("Failed to process %s: %s", url, exc)
                self._record_page_failure(url, detail=short_error(exc))
                discovered = []

            self.recorder.pages_checked += 1
            await self.recorder.flush(current_url=url)
            return discovered

    def _record_page_failure(
        self, url: str, *, status_code: Optional[int] = None, detail: str
    ) -> None:
        if url == self.start_url:
            self.root_failure = f"Could not load the starting page: {detail}"
        if not self._dedupe(url, url, IssueType.broken_link):
            return
        self.recorder.add_issue(
            source_url=url,
            target_url=url,
            resource_type=ResourceType.page,
            issue_type=IssueType.broken_link,
            status_code=status_code,
            detail=detail,
        )

    async def _process_page(
        self, context: Any, api_request: Any, url: str, depth: int
    ) -> list[str]:
        page = await context.new_page()
        try:
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.config.page_timeout_ms
                )
            except PlaywrightError as exc:
                self._record_page_failure(url, detail=short_error(exc))
                return []

            if response is not None and response.status >= 400:
                self._record_page_failure(
                    url,
                    status_code=response.status,
                    detail=describe_failure(UrlCheck(status=response.status, ok=False)),
                )
                return []

            assets: dict[str, Any] = await page.evaluate(EXTRACT_ASSETS_JS)
        finally:
            await page.close()

        links = self._collect_links(assets.get("links", []))
        images = assets.get("images", [])

        internal_links = [u for u in links if is_same_site(u, self.start_url)]
        self.recorder.links_checked += len(links)

        link_results, _ = await asyncio.gather(
            self._check_links(api_request, url, links),
            self._check_images(api_request, url, images),
        )

        if depth >= self.config.max_depth:
            return []

        return [
            link
            for link in internal_links
            if link_results.get(link, False) and link not in self.visited_pages
        ]

    def _collect_links(self, raw_links: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        links: list[str] = []
        for raw in raw_links:
            normalized = normalize_url(raw)
            if normalized and normalized not in seen:
                seen.add(normalized)
                links.append(normalized)
        return links

    async def _check_links(
        self, api_request: Any, source_url: str, links: list[str]
    ) -> dict[str, bool]:
        async def check(link: str) -> tuple[str, UrlCheck]:
            return link, await self.check_url(api_request, link)

        results = await asyncio.gather(*(check(link) for link in links))

        crawlable: dict[str, bool] = {}
        for link, result in results:
            crawlable[link] = result.ok and "html" in (result.content_type or "").lower()
            if result.ok:
                continue
            if not self._dedupe(source_url, link, IssueType.broken_link):
                continue
            self.recorder.add_issue(
                source_url=source_url,
                target_url=link,
                resource_type=ResourceType.link,
                issue_type=IssueType.broken_link,
                status_code=result.status,
                detail=describe_failure(result),
            )
        return crawlable

    async def _check_images(
        self, api_request: Any, source_url: str, images: list[dict[str, Any]]
    ) -> None:
        to_check: list[str] = []

        for image in images:
            src = (image.get("src") or "").strip()
            has_alt = bool(image.get("hasAlt"))
            alt_text = (image.get("alt") or "").strip()
            label = src or "<img> without src"

            self.recorder.images_checked += 1

            if not has_alt or not alt_text:
                alt_status = AltStatus.missing if not has_alt else AltStatus.empty
                if self._dedupe(source_url, label, IssueType.missing_alt, alt_status.value):
                    self.recorder.add_issue(
                        source_url=source_url,
                        target_url=label,
                        resource_type=ResourceType.image,
                        issue_type=IssueType.missing_alt,
                        alt_status=alt_status,
                        detail=(
                            "alt attribute is absent"
                            if not has_alt
                            else "alt attribute is present but empty"
                        ),
                    )

            normalized = normalize_url(src)
            if normalized:
                to_check.append(normalized)
            elif not src and self._dedupe(source_url, label, IssueType.broken_image):
                self.recorder.add_issue(
                    source_url=source_url,
                    target_url=label,
                    resource_type=ResourceType.image,
                    issue_type=IssueType.broken_image,
                    detail="Image has no src attribute",
                )

        async def check(src: str) -> tuple[str, UrlCheck]:
            return src, await self.check_url(api_request, src)

        results = await asyncio.gather(*(check(src) for src in dict.fromkeys(to_check)))

        for src, result in results:
            if result.ok:
                continue
            if not self._dedupe(source_url, src, IssueType.broken_image):
                continue
            self.recorder.add_issue(
                source_url=source_url,
                target_url=src,
                resource_type=ResourceType.image,
                issue_type=IssueType.broken_image,
                status_code=result.status,
                detail=describe_failure(result),
            )


async def run_scan(scan_id: int) -> None:
    """Entry point used by the API background task."""
    with Session(engine) as session:
        scan = session.exec(select(Scan).where(Scan.id == scan_id)).first()
        if scan is None:
            logger.error("Scan %s not found", scan_id)
            return
        start_url = scan.url
        config = CrawlConfig(max_depth=scan.max_depth, max_pages=scan.max_pages)

    crawler = SiteCrawler(scan_id, start_url, config)
    try:
        await crawler.run()
    except Exception as exc:  # noqa: BLE001 - surface any crawl failure on the scan record
        logger.exception("Scan %s failed", scan_id)
        crawler.recorder.discard_pending()
        with Session(engine) as session:
            scan = session.get(Scan, scan_id)
            if scan is not None:
                scan.status = ScanStatus.failed
                scan.error = short_error(exc)
                scan.finished_at = utcnow()
                scan.current_url = None
                session.add(scan)
                session.commit()
