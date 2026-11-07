"""Backwards-compatible launcher for the BookLore MCP server.

The application now lives in the `src/booklore_mcp` package. This thin shim keeps the
old invocations working:

    uv run server.py                 # run the server
    uv run fastmcp list server.py    # fastmcp discovers the `mcp` object below

Prefer `booklore-mcp` (the installed console script) or `python -m booklore_mcp`.
"""

from __future__ import annotations

from booklore_mcp import main, mcp  # noqa: F401  (fastmcp discovers `mcp`)

if __name__ == "__main__":
    main()
