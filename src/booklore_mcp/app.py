"""The FastMCP application: the shared `mcp` instance, its lifespan, and `main()`.

This module deliberately does NOT import the `tools` package — that would create an
import cycle (every tool module imports `mcp` from here). Tool registration happens as
an import side-effect of the `tools` package, which the package __init__ / __main__
pull in before the server runs.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from . import config, runtime
from .log import configure_logging, get_logger

log = get_logger("booklore")


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Close the shared HTTP client when the server shuts down."""
    try:
        yield
    finally:
        await runtime.client.close()


mcp = FastMCP(
    name="booklore",
    lifespan=lifespan,
    instructions=(
        "Tools for a self-hosted BookLore library. Book and shelf IDs are not "
        "guessable — use list_books / list_shelves to discover them before "
        "calling tools that take an ID. list_books returns trimmed summaries; "
        "call get_book for the full record including metadata. To set reading "
        "progress you need a book_file_id — read it from get_book's primaryFile."
    ),
)


def main() -> None:
    """Console entry point. Transport is selectable via env: defaults to a
    long-lived HTTP server; set MCP_TRANSPORT=stdio for a local process that an
    MCP host (Claude Desktop/Code) launches directly."""
    # Importing the package registers every tool on `mcp` (import side-effect).
    from . import tools  # noqa: F401

    configure_logging()
    transport = os.environ.get("MCP_TRANSPORT", "http")
    if transport == "stdio":
        log.info("starting booklore mcp server", transport="stdio", booklore_url=config.BASE_URL)
        mcp.run()  # stdio
    else:
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8000"))
        path = os.environ.get("MCP_PATH", "/mcp")
        log.info(
            "starting booklore mcp server",
            transport="http",
            host=host,
            port=port,
            path=path,
            booklore_url=config.BASE_URL,
        )
        mcp.run(transport="http", host=host, port=port, path=path)
