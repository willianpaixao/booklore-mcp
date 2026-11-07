"""MCP tool modules.

Importing this package imports every tool module, and each module registers its
functions on the shared `mcp` instance via the `@mcp.tool` decorator. Nothing here is
meant to be called directly — importing for the registration side-effect is the point.
"""

from __future__ import annotations

from . import (
    books,
    libraries,
    metadata,
    reading,
    search,
    series,
    shelves,
    stats,
)

__all__ = [
    "books",
    "libraries",
    "metadata",
    "reading",
    "search",
    "series",
    "shelves",
    "stats",
]
