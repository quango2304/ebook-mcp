from __future__ import annotations

import os
import re
import time
import base64
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urljoin, quote

import httpx
from bs4 import BeautifulSoup

from ebook_mcp.models import EbookResult, SearchResults

logger = logging.getLogger(__name__)

SOURCE_URL = "https://annas-archive.gl"
SOURCE_NAME = "annas-archive"
EXACT_PAGE_COUNT = False  # pagination only lists pages near the current one
FETCH_ATTEMPTS = 3  # retries for flaky/partially-loaded pages


# ---------------------------------------------------------------------------
# 2captcha — image captcha solver
# ---------------------------------------------------------------------------

def _solve_captcha_image(image_bytes: bytes) -> str | None:
    api_key = os.environ.get("CAPTCHA_API_KEY", "").strip()
    if not api_key:
        logger.warning("Image captcha shown but CAPTCHA_API_KEY is not set; cannot solve it")
        return None
    try:
        resp = httpx.post("https://2captcha.com/in.php", data={
            "key": api_key,
            "method": "post",
            "json": "1",
        }, files={"file": ("captcha.png", image_bytes, "image/png")}, timeout=30.0)
        result = resp.json()
        if result.get("status") != 1:
            logger.warning("2captcha submit failed: %s", result)
            return None
        captcha_id = result["request"]

        for _ in range(30):
            time.sleep(3)
            r2 = httpx.get("https://2captcha.com/res.php", params={
                "key": api_key,
                "action": "get",
                "id": captcha_id,
                "json": "1",
            }, timeout=15.0)
            res = r2.json()
            if res.get("status") == 1:
                return res["request"]
            if res.get("request") != "CAPCHA_NOT_READY":
                logger.warning("2captcha error: %s", res)
                return None
    except Exception as e:
        logger.warning("2captcha exception: %s", e)
    return None


# ---------------------------------------------------------------------------
# Playwright (sync) — DDoS-Guard bypass
# ---------------------------------------------------------------------------

_sync_playwright = None
_sync_browser = None


def _get_sync_browser():
    global _sync_playwright, _sync_browser
    if _sync_browser is None:
        from playwright.sync_api import sync_playwright
        _sync_playwright = sync_playwright().start()
        _sync_browser = _sync_playwright.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ])
    return _sync_browser


def _handle_ddosguard_sync(page) -> None:
    from playwright.sync_api import TimeoutError as PWTimeout

    for attempt in range(4):
        try:
            # Case 1: automatic JS challenge ("wait a few seconds") — just wait
            content = page.content()
            if "wait a few seconds" in content.lower():
                logger.info("Auto JS challenge detected, waiting for it to resolve...")
                page.wait_for_timeout(15000)
                # Wait for navigation to the real page
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except PWTimeout:
                    pass
                content = page.content()
                if "ddos-guard" not in content.lower() or len(content) > 10000:
                    logger.info("Auto challenge passed")
                    return

            # Case 2: manual checkbox challenge
            checkbox = page.locator(".ddg-captcha__checkbox, #ddg-captcha, .ddg-captcha")
            if checkbox.count() > 0:
                logger.info("Checkbox challenge detected, clicking...")
                try:
                    checkbox.first.click(timeout=5000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(8000)

                # Image captcha
                captcha_img = page.locator(".ddg-modal__captcha-image")
                if captcha_img.count() > 0:
                    logger.info("Image captcha detected, solving with 2captcha...")
                    src = captcha_img.first.get_attribute("src")
                    if src and src.startswith("data:image"):
                        image_bytes = base64.b64decode(src.split(",", 1)[1])
                    elif src:
                        image_bytes = httpx.get(urljoin(SOURCE_URL, src), timeout=30.0).content
                    else:
                        image_bytes = captcha_img.first.screenshot()

                    if image_bytes:
                        solution = _solve_captcha_image(image_bytes)
                        if solution:
                            page.locator(".ddg-modal__input").first.fill(solution)
                            page.locator(".ddg-modal__submit").first.click()
                            page.wait_for_timeout(8000)

            content = page.content()
            if "ddos-guard" not in content.lower() or len(content) > 10000:
                logger.info("DDoS-Guard challenge passed")
                return
        except Exception as e:
            logger.warning("DDoS-Guard attempt %d failed: %s", attempt + 1, e)
        page.wait_for_timeout(3000)


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# Sync Playwright is bound to the thread that started it, so every browser
# call runs on this single dedicated worker thread (which also serialises them).
_browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")


@contextmanager
def _open_page(url: str):
    """Open `url` in a fresh browser context, passing DDoS-Guard if needed."""
    context = _get_sync_browser().new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    try:
        logger.info("Navigating to: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        content = page.content()
        if "ddos-guard" in content.lower() and len(content) < 10000:
            _handle_ddosguard_sync(page)
        yield page
    finally:
        context.close()


def _fetch_page_sync(url: str) -> str:
    with _open_page(url) as page:
        return page.content()


async def _fetch_page(url: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_browser_executor, _fetch_page_sync, url)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_META_SPLIT = re.compile(r"\s*·\s*")
_YEAR = re.compile(r"^(?:1[5-9]|20)\d{2}$")
_SIZE = re.compile(r"^\d+(?:\.\d+)?\s*(?:KB|MB|GB)$", re.I)
_LANG = re.compile(r"\[([A-Za-z-]{2,8})\]")
_FORMATS = {"pdf", "epub", "mobi", "azw3", "djvu", "cbz", "cbr", "fb2", "txt", "rtf", "docx", "doc", "lit", "zip", "rar"}


def _parse_meta_line(text: str) -> dict:
    """Parse e.g. 'English [en] · EPUB · 2.2MB · 2022 · 📕 Book (fiction) · 🚀/lgli/lgrs · Save · 194'."""
    meta = {"language": "", "file_type": "", "size": "", "year": "", "content_type": "", "sources": ""}
    for part in _META_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        lang = _LANG.search(part)
        if lang and not meta["language"]:
            meta["language"] = lang.group(1).lower()
        elif part.lower() in _FORMATS:
            meta["file_type"] = part.lower()
        elif _SIZE.match(part):
            meta["size"] = part.upper().replace(" ", "")
        elif _YEAR.match(part):
            meta["year"] = part
        elif part.startswith("🚀"):
            meta["sources"] = part.lstrip("🚀").strip()
        elif re.match(r"^[\U0001F000-\U0001FFFF\u2600-\u27BF]", part) and not meta["content_type"]:
            meta["content_type"] = re.sub(r"^[^\w(]+", "", part).strip()
    return meta


def _parse_result_item(item) -> EbookResult | None:
    link = item.select_one("a[href*='/md5/']")
    md5_match = re.search(r"/md5/([a-f0-9]{32})", link.get("href", "")) if link else None
    if not md5_match:
        return None
    md5 = md5_match.group(1)

    covers = item.select("div[data-content]")
    title_link = item.select_one("a.js-vim-focus") or link
    title = title_link.get_text(strip=True) or (covers[0]["data-content"].strip() if covers else "")

    author = ""
    publisher = ""
    for a in item.select("a[href^='/search?q=']"):
        icon = a.select_one("span[class*='icon-']")
        icon_cls = " ".join(icon.get("class", [])) if icon else ""
        text = a.get_text(" ", strip=True)
        if "user-edit" in icon_cls and not author:
            author = text
        elif "company" in icon_cls and not publisher:
            publisher = text
    if not author and len(covers) >= 2:
        author = covers[1]["data-content"].strip()

    path_el = item.select_one("div.font-mono")
    description_el = item.select_one("div.line-clamp-\\[2\\].text-gray-600, div.text-gray-600")
    meta_el = item.select_one("div.text-gray-800, div.font-semibold.text-sm")
    meta = _parse_meta_line(meta_el.get_text(" ", strip=True)) if meta_el else {}

    downloads_el = item.select_one("[title='Downloads']")
    downloads = 0
    if downloads_el:
        dm = re.search(r"([\d,]+)\s*$", downloads_el.get_text(" ", strip=True))
        if dm:
            downloads = int(dm.group(1).replace(",", ""))

    # Year also appears at the end of the publisher line ("..., 2022")
    year = meta.get("year", "")
    if not year:
        ym = re.search(r"\b((?:1[5-9]|20)\d{2})\b\s*$", publisher)
        year = ym.group(1) if ym else ""

    return EbookResult(
        source=SOURCE_NAME, md5=md5, title=title, path=f"/md5/{md5}", author=author, publisher=publisher,
        year=year, language=meta.get("language", ""), file_type=meta.get("file_type", ""),
        size=meta.get("size", ""), content_type=meta.get("content_type", ""),
        sources=meta.get("sources", ""), downloads=downloads,
        filename=path_el.get_text(strip=True) if path_el else "",
        description=description_el.get_text("\n", strip=True) if description_el else "",
    )


def _parse_search_html(html: str, query: str) -> SearchResults:
    soup = BeautifulSoup(html, "lxml")

    # Remove the "recent downloads" sidebar so it doesn't pollute results
    for sidebar in soup.select(".js-recent-downloads-container, .js-recent-downloads-scroll"):
        sidebar.decompose()

    results: list[EbookResult] = []
    seen: set[str] = set()
    for item in soup.select("div.flex.pt-3.pb-3.border-b"):
        result = _parse_result_item(item)
        if result and result.md5 not in seen:
            seen.add(result.md5)
            results.append(result)

    page_nums = {
        int(m.group(1))
        for a in soup.select("a[href*='page=']")
        if (m := re.search(r"page=(\d+)", a.get("href", "")))
    }
    return SearchResults(query=query, results=results, total_pages=max(page_nums, default=1))


async def search_ebooks(query: str, page: int = 1) -> SearchResults:
    search_url = f"{SOURCE_URL}/search?index=&page={page}&q={quote(query)}&display=&sort="
    sr = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        sr = _parse_search_html(await _fetch_page(search_url), query)
        if sr.results:
            break
        logger.warning("Search returned no results (attempt %d/%d)", attempt, FETCH_ATTEMPTS)
    sr.current_page = page
    sr.total_pages = max(sr.total_pages, page)
    sr.has_next = bool(sr.results) and page < sr.total_pages
    return sr
