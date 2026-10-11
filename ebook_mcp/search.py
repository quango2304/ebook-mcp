from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ebook_mcp import annas_archive, oceanofpdf
from ebook_mcp.models import EbookResult, SearchResults

logger = logging.getLogger(__name__)

SOURCES = {
    annas_archive.SOURCE_NAME: annas_archive,
    oceanofpdf.SOURCE_NAME: oceanofpdf,
}

# Remember how many pages each source has per query, so asking for a page beyond a
# source's last page doesn't cost a slow request. Only for sources whose page count is
# exact (EXACT_PAGE_COUNT); bounded to keep memory small.
_page_counts: dict[tuple[str, str], int] = {}
_PAGE_COUNT_LIMIT = 500


@dataclass
class CombinedResults:
    query: str
    results: list[EbookResult] = field(default_factory=list)
    total_pages: int = 0
    current_page: int = 1
    has_next: bool = False
    warnings: list[str] = field(default_factory=list)


async def _search_source(name: str, query: str, page: int) -> SearchResults:
    source = SOURCES[name]
    exact = source.EXACT_PAGE_COUNT
    known_total = _page_counts.get((name, query)) if exact else None
    if known_total is not None and page > known_total:
        return SearchResults(query=query, current_page=page, total_pages=known_total)

    sr = await source.search_ebooks(query, page)
    if exact and (sr.total_pages or page == 1):  # a page-1 result is authoritative, even when it is 0
        if len(_page_counts) >= _PAGE_COUNT_LIMIT:
            _page_counts.clear()
        _page_counts[(name, query)] = sr.total_pages
    return sr


async def search_all(query: str, page: int = 1) -> CombinedResults:
    """Search every source and merge: page N = page N of each source.

    total_pages is the largest page count among the sources; a source that has run
    out of pages simply contributes nothing.
    """
    names = list(SOURCES)
    outcomes = await asyncio.gather(*(_search_source(n, query, page) for n in names), return_exceptions=True)

    combined = CombinedResults(query=query, current_page=page)
    failures = 0
    for name, outcome in zip(names, outcomes):
        if isinstance(outcome, BaseException):
            failures += 1
            logger.error("Source %s failed: %s", name, outcome)
            combined.warnings.append(f"one source was unavailable ({type(outcome).__name__})")
            continue
        combined.results += outcome.results
        combined.total_pages = max(combined.total_pages, outcome.total_pages)

    if failures == len(names):
        raise RuntimeError("all sources failed")
    combined.has_next = page < combined.total_pages
    return combined
