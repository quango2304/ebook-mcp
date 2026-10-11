from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from mcp.server.mcpserver import MCPServer

from ebook_mcp import annas_archive, oceanofpdf
from ebook_mcp.config import API_KEY, PUBLIC_URL
from ebook_mcp.models import EbookResult
from ebook_mcp.search import search_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ebook-mcp")

DESCRIPTION_LIMIT = 600

# source name -> (our URL prefix, real site). Opening <PUBLIC_URL><prefix>/<path> redirects to <site>/<path>.
REDIRECTS = {
    annas_archive.SOURCE_NAME: ("/aa", annas_archive.SOURCE_URL),
    oceanofpdf.SOURCE_NAME: ("/op", oceanofpdf.SOURCE_URL),
}


def detail_link(result: EbookResult) -> str:
    """Our own link for a book; opening it redirects to the real detail page."""
    return f"{PUBLIC_URL}{REDIRECTS[result.source][0]}{result.path}"


def format_result(index: int, r: EbookResult) -> str:
    lines = [f"{index}. **{r.title}**", f"   - Link: {detail_link(r)}"]
    fields = [
        ("Author", r.author),
        ("Publisher", r.publisher),
        ("Year", r.year),
        ("Language", r.language),
        ("Format", r.file_type.upper()),
        ("Size", r.size),
        ("Type", r.content_type),
        ("Genre", r.genre),
        ("Date added", r.date_added),
        ("Sources", r.sources),
        ("Downloads", f"{r.downloads:,}" if r.downloads else ""),
        ("Filename", r.filename),
        ("MD5", r.md5),
    ]
    lines += [f"   - {label}: {value}" for label, value in fields if value]
    if r.description:
        text = " ".join(r.description.split())
        if len(text) > DESCRIPTION_LIMIT:
            text = text[:DESCRIPTION_LIMIT].rstrip() + "…"
        lines.append(f"   - Description: {text}")
    return "\n".join(lines)


mcp = MCPServer(name="ebook-mcp", instructions="Search ebooks from your personal vault")


@mcp.tool(name="search_ebooks")
async def search(query: str, page: int = 1) -> str:
    """Search ebooks by title, author, ISBN or keyword, across all sources at once.

    Args:
        query: Search query (title, author, ISBN or keyword)
        page: Page number (default: 1). Page N holds page N of every source, so a page can
            hold well over 50 books; the total page count is that of the largest source.

    Returns a list of books. Each book has: title, a link to its detail page, and whatever
    of author, publisher, year, language, format, size, type, genre, date added, sources,
    download count, filename, MD5 and description is known.
    """
    if not query.strip():
        return "Error: query is required"
    try:
        sr = await search_all(query.strip(), max(page, 1))
    except Exception as e:
        logger.exception("Search failed")
        return f"Search failed: {e}"

    if not sr.results:
        return f"No results found for '{query}'."

    lines = [f'**Search**: "{query}" — page {sr.current_page} of {sr.total_pages}, {len(sr.results)} results', ""]
    lines += [f"> Note: {w}" for w in dict.fromkeys(sr.warnings)]
    lines += [format_result(i, r) + "\n" for i, r in enumerate(sr.results, 1)]
    if sr.has_next:
        lines.append(f"> More results: call again with page={sr.current_page + 1}.")
    return "\n".join(lines)


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="0.0.0.0",
    json_response=False,
)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            key = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ")
            if key != API_KEY:
                return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        return await call_next(request)


app.add_middleware(ApiKeyMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def make_redirect(site: str):
    async def redirect_to_source(request: Request):
        path = request.path_params.get("path", "")
        if not path:
            return HTMLResponse("<h1>Missing path</h1>", status_code=400)
        return RedirectResponse(url=f"{site}/{path}", status_code=302)

    return redirect_to_source


app.routes.insert(0, Route("/health", health, methods=["GET"]))
for i, (prefix, site) in enumerate(REDIRECTS.values(), start=1):
    app.routes.insert(i, Route(prefix + "/{path:path}", make_redirect(site), methods=["GET"]))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ebook_mcp.server:app", host="0.0.0.0", port=8000, reload=True)