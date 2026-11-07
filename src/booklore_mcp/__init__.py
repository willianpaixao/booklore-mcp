"""BookLore MCP server.

Wraps a self-hosted BookLore instance's REST API (`/api/v1/...`) so an MCP host can
search a library, read and edit metadata, manage shelves, and track reading progress.

Importing this package builds the shared `mcp` server and registers every tool (the
`tools` import below has that side-effect). Use `main()` as the console entry point.
"""

from __future__ import annotations

from . import prompts, tools  # noqa: F401  (import for the registration side-effect)
from .app import main, mcp

__all__ = ["main", "mcp"]
