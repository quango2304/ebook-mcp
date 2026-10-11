from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from ebook_mcp.models import EbookResult, SearchResults

logger = logging.getLogger(__name__)

SOURCE_URL = "https://oceanofpdf.com"
SOURCE_NAME = "oceanofpdf"
EXACT_PAGE_COUNT = True  # pagination exposes the true last page
# FlareSolverr container (see docker-compose.yml) solves the Cloudflare challenge.
FLARESOLVERR_URL = "http://flaresolverr:8191/v1"
FETCH_ATTEMPTS = 3

# Maps the labels in the "postmetainfo" block to EbookResult fields.
_META_FIELDS = {
    "author": "author",
    "language": "language",
    "genre": "genre",
    "publisher": "publisher",
    "year": "year",
    "format": "file_type",
    "file size": "size",
    "size": "size",
}


async def _fetch(url: str) -> tuple[int, str]:
    """GET `url` through FlareSolverr; returns (http status, html)."""
    payload = {"cmd": "request.get", "url": url, "session": "ebook-mcp", "maxTimeout": 60000}
    last_error = "unknown error"
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(FLARESOLVERR_URL, json=payload)
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            last_error = f"FlareSolverr unreachable: {e}"
        else:
            solution = data.get("solution") or {}
            if data.get("status") == "ok" and solution.get("response"):
                return solution.get("status", 200), solution["response"]
            last_error = data.get("message", "FlareSolverr failed")
        logger.warning("OceanofPDF fetch attempt %d/%d failed: %s", attempt, FETCH_ATTEMPTS, last_error)
    raise RuntimeError(last_error)


def _parse_meta(article) -> dict[str, str]:
    """Parse '<strong>Author:</strong> Name <br> <strong>Language:</strong> English <br>'."""
    meta: dict[str, str] = {}
    block = article.select_one(".postmetainfo")
    if not block:
        return meta
    for label in block.select("strong"):
        parts = []
        for sibling in label.next_siblings:
            if getattr(sibling, "name", None) in ("br", "strong"):
                break
            parts.append(sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip())
        value = " ".join(p for p in parts if p).strip()
        key = label.get_text(strip=True).rstrip(":").lower()
        if value and key in _META_FIELDS:
            meta[_META_FIELDS[key]] = value
    return meta


def _parse_article(article) -> EbookResult | None:
    link = article.select_one("h2.entry-title a") or article.select_one(".entry-title a")
    if not link or not link.get("href"):
        return None

    meta = _parse_meta(article)
    label = article.get("aria-label", "")
    fmt = re.match(r"\[(\w+)\]", label)
    time_el = article.select_one("time.entry-time")

    description = ""
    content = article.select_one(".entry-content")
    if content:
        for more in content.select(".more-link"):
            more.decompose()
        description = content.get_text(" ", strip=True)

    return EbookResult(
        source=SOURCE_NAME,
        title=link.get_text(strip=True),
        path=urlparse(link["href"]).path,
        author=meta.get("author", ""),
        publisher=meta.get("publisher", ""),
        year=meta.get("year", ""),
        language=meta.get("language", ""),
        file_type=meta.get("file_type", fmt.group(1).lower() if fmt else ""),
        size=meta.get("size", ""),
        genre=meta.get("genre", ""),
        date_added=time_el.get_text(strip=True) if time_el else "",
        description=description,
    )


def _parse_search_html(html: str, query: str) -> SearchResults:
    soup = BeautifulSoup(html, "lxml")
    results = [r for a in soup.select("article.entry") if (r := _parse_article(a))]

    pages = {
        int(m.group(1))
        for a in soup.select(".archive-pagination a[href]")
        if (m := re.search(r"/page/(\d+)/", a["href"]))
    }
    current = soup.select_one(".archive-pagination li.active")
    if current and (m := re.search(r"\d+", current.get_text())):
        pages.add(int(m.group()))
    return SearchResults(query=query, results=results, total_pages=max(pages, default=1 if results else 0))


async def search_ebooks(query: str, page: int = 1) -> SearchResults:
    q = quote_plus(query)
    url = f"{SOURCE_URL}/?s={q}" if page <= 1 else f"{SOURCE_URL}/page/{page}/?s={q}"
    status, html = await _fetch(url)
    sr = _parse_search_html(html, query)
    if status == 404 or not sr.results:  # past the last page (or nothing found)
        sr.results, sr.total_pages = [], 0
    sr.current_page = page
    sr.has_next = page < sr.total_pages
    return sr
