# ebook-mcp

An MCP server with one tool, `search_ebooks`, that searches **Anna's Archive** and **OceanofPDF** at
once and returns each book's metadata (title, author, publisher, year, language, format, size, genre,
description, …) plus a link on your own server that redirects to the book's detail page:

| Link prefix | Redirects to |
|---|---|
| `<PUBLIC_URL>/aa/md5/<md5>` | Anna's Archive |
| `<PUBLIC_URL>/op/authors/<author>/<slug>/` | OceanofPDF |

Page *N* returns page *N* of both sites, merged; the total page count is the larger of the two.
The source sites' domains never appear in tool output. No download links are provided.

## 1. Create `.env`

```bash
git clone https://github.com/quango2304/ebook-mcp.git
cd ebook-mcp
cp .env.example .env
```

Edit `.env`:

| Variable | Meaning |
|---|---|
| `MCP_API_KEY` | Secret your MCP client sends in the `X-API-Key` header. Generate one: `openssl rand -hex 24` |
| `PUBLIC_URL` | Address you reach the server on, e.g. `http://localhost:8001` or `http://<vps-ip>:8001`. Links in results are built from it |
| `PORT` | Host port to publish (default `8001`; keep it in sync with `PUBLIC_URL`) |
| `CAPTCHA_API_KEY` | Optional [2captcha](https://2captcha.com) key, only used if Anna's Archive shows an image captcha. Leave empty to disable |

`.env` is git-ignored — never commit it.

## 2. Run

```bash
docker compose up -d --build
curl http://localhost:8001/health      # {"status":"ok"}
```

This starts the server plus a [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) container
(needed for OceanofPDF's Cloudflare check). Update later with `git pull && docker compose up -d --build`.

## 3. Connect Claude CLI

Use the same `MCP_API_KEY` and `PUBLIC_URL` as in `.env`:

```bash
claude mcp add --scope user --transport http ebook http://localhost:8001/mcp \
  --header "X-API-Key: <your MCP_API_KEY>"
claude mcp list        # ebook ... ✔ Connected
```

Then ask Claude, e.g. *"search ebooks for dune frank herbert"*.

**Server on a VPS?** Either open the port and use `http://<vps-ip>:8001` for both `PUBLIC_URL` and the
`claude mcp add` URL, or keep the port private and tunnel it
(`ssh -N -L 8001:localhost:8001 user@your-vps`), leaving `http://localhost:8001` everywhere.

## Notes

- A search takes ~20 s: Anna's Archive sits behind DDoS-Guard, so the server drives headless Chromium
  (Playwright) and waits out its JS challenge.
- If one site is down, the other's results are still returned, with a note.
- Tests (server running): `pip install httpx && python tests/e2e.py`
