"""End-to-end tests against a running ebook-mcp server.

Usage: python tests/e2e.py [base_url]     (default: PUBLIC_URL from .env)
Reads MCP_API_KEY / PUBLIC_URL from the environment or the .env file. Requires: pip install httpx
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
API_KEY = os.environ["MCP_API_KEY"]
PUBLIC = os.environ.get("PUBLIC_URL", "http://localhost:8001").rstrip("/")  # prefix of links in results
BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PUBLIC_URL", "http://localhost:8001")).rstrip("/")
HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_ids = iter(range(1, 10_000))
results: list[tuple[str, bool]] = []


def rpc(method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or {}}
    r = httpx.post(f"{BASE}/mcp", json=body, headers=HEADERS, timeout=240)
    m = re.search(r"^data: (.*)$", r.text, re.M)
    return json.loads(m.group(1) if m else r.text)


def search(query: str, page: int = 1) -> str:
    d = rpc("tools/call", {"name": "search_ebooks", "arguments": {"query": query, "page": page}})
    return d["result"]["content"][0]["text"]


def check(name: str, ok: bool, info: str = ""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}", flush=True)


def books(text: str) -> list[str]:
    return re.split(r"\n(?=\d+\. \*\*)", text)[1:]


def main():
    check("health", httpx.get(f"{BASE}/health").json() == {"status": "ok"})
    bad = httpx.post(f"{BASE}/mcp", json={}, headers={**HEADERS, "X-API-Key": "nope"})
    check("auth rejects bad key", bad.status_code == 401)
    no_key = httpx.post(f"{BASE}/mcp", json={}, headers={k: v for k, v in HEADERS.items() if k != "X-API-Key"})
    check("auth rejects missing key", no_key.status_code == 401)
    names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    check("exactly one tool: search_ebooks", names == ["search_ebooks"], str(names))

    link_re = re.compile(rf"Link: {re.escape(PUBLIC)}/(?:aa/md5/[a-f0-9]{{32}}\b|op/authors/\S+/)")
    leak_re = re.compile(r"annas-archive|oceanofpdf", re.I)
    first_link = ""
    for q in ["dune frank herbert", "the pragmatic programmer", "clean code robert martin",
              "sapiens harari", "cuốn theo chiều gió", "harry potter philosopher's stone",
              "9780441172719"]:
        t0 = time.time()
        text = search(q)
        bs = books(text)
        ok = (
            len(bs) > 0
            and all(link_re.search(b) for b in bs)
            and not leak_re.search(text)
            and sum("Format:" in b for b in bs) > len(bs) * 0.8
            and sum("Size:" in b for b in bs) > len(bs) * 0.8
            and sum("Author:" in b for b in bs) > len(bs) * 0.6
        )
        check(f"search '{q}'", ok, f"{len(bs)} books, {time.time() - t0:.0f}s")
        if not first_link and bs:
            first_link = re.search(r"Link: (\S+)", bs[0]).group(1).replace(PUBLIC, BASE, 1)

    # --- both sources are merged: page N = page N of each ---
    t1 = search("an introduction of")
    check("page 1 merges both sources", "/aa/md5/" in t1 and "/op/authors/" in t1,
          f"{t1.count('/aa/md5/')} + {t1.count('/op/authors/')}")
    check("total pages is the larger source", "of 78," in t1 or int(re.search(r"of (\d+),", t1).group(1)) > 20,
          re.search(r"page \d+ of \d+", t1).group(0))
    t2 = search("an introduction of", 2)
    oc1 = set(re.findall(r"/op/authors/\S+", t1))
    oc2 = set(re.findall(r"/op/authors/\S+", t2))
    check("page 2 has different books from both sources", bool(oc2) and not (oc1 & oc2) and "/aa/md5/" in t2)

    # 'dune frank herbert' has far fewer OceanofPDF pages than Anna's Archive pages:
    # deep pages must still return Anna's Archive results, and page 11 must not be cut off.
    deep = search("dune frank herbert", page=11)
    check("page beyond the smaller source still returns the larger", "/aa/md5/" in deep and "/op/authors/" not in deep,
          deep.splitlines()[0][:60])
    check("OceanofPDF fields parsed", "Genre:" in t1 and "Date added:" in t1 and "Format: PDF" in t1)

    with cf.ThreadPoolExecutor(4) as ex:
        outs = list(ex.map(search, ["neuromancer", "foundation asimov", "the hobbit", "dracula"]))
    check("4 concurrent searches", all(books(o) for o in outs))

    check("empty query", "required" in search("   ").lower())
    check("gibberish handled", "Search failed" not in search("zzqxjkvv qwpoeiru lkjhg 98172"))
    check("special chars handled", "Search failed" not in search('c++ "design patterns" & more/ö'))

    r = httpx.get(first_link, follow_redirects=False)
    check("annas detail link redirects to source", r.status_code == 302 and "/md5/" in r.headers.get("location", ""),
          r.headers.get("location", ""))
    oc_link = re.search(rf"Link: ({re.escape(PUBLIC)}/op/\S+)", t1).group(1).replace(PUBLIC, BASE, 1)
    r = httpx.get(oc_link, follow_redirects=False)
    check("oceanofpdf detail link redirects to source",
          r.status_code == 302 and r.headers.get("location", "").startswith("https://oceanofpdf.com/authors/"),
          r.headers.get("location", "")[:70])

    fails = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
