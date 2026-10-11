"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


# Secret the MCP client must send in the X-API-Key header.
API_KEY = _require("MCP_API_KEY")
# Address this server is reached on; detail links in search results are built from it.
PUBLIC_URL = _require("PUBLIC_URL").rstrip("/")
