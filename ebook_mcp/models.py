from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EbookResult:
    source: str  # "annas-archive" | "oceanofpdf"
    title: str
    path: str  # site-relative detail path, e.g. /md5/<md5>
    author: str = ""
    publisher: str = ""
    year: str = ""
    language: str = ""
    file_type: str = ""
    size: str = ""
    content_type: str = ""
    genre: str = ""
    sources: str = ""
    downloads: int = 0
    filename: str = ""
    date_added: str = ""
    md5: str = ""
    description: str = ""


@dataclass
class SearchResults:
    query: str
    results: list[EbookResult] = field(default_factory=list)
    total_pages: int = 0
    current_page: int = 1
    has_next: bool = False
