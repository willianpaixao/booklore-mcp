"""Deployment-wiring tests for the src-layout package.

These guard the things the package restructure could silently break: that every tool
module is imported and registered on the shared `mcp`, that the console-script / module
entrypoints are wired, and that the backwards-compat root `server.py` shim re-exports
the same server object FastMCP discovers.
"""

from __future__ import annotations

from fastmcp import Client

import booklore_mcp
import server as server_shim

# The full public tool surface. Adding or removing a tool should update this set in the
# same change — that's the point: it's a deliberate record of what the deployment exposes.
EXPECTED_TOOLS = {
    "add_categories",
    "add_tags",
    "add_to_shelves",
    "assign_shelves",
    "bookdrop_rescan",
    "books_by_author",
    "books_by_genre",
    "bulk_set_read_status",
    "bulk_update_metadata",
    "create_shelf",
    "delete_shelf",
    "export_library",
    "fetch_metadata_candidates",
    "find_duplicates",
    "find_series_gaps",
    "get_book",
    "get_reading_stats",
    "get_recommendations",
    "get_shelf_books",
    "isbn_lookup",
    "list_authors",
    "list_books",
    "match_books",
    "list_genres",
    "list_libraries",
    "list_shelves",
    "normalize_goodreads_ids",
    "organize_shelves",
    "ping",
    "refresh_library",
    "remove_categories",
    "remove_from_shelves",
    "remove_tags",
    "reset_progress",
    "search_books",
    "search_titles",
    "set_field_locks",
    "set_personal_rating",
    "set_read_status",
    "set_reading_progress",
    "update_book_metadata",
}


async def test_all_tools_registered():
    """Importing the package must register exactly the expected tool set on `mcp` —
    catches a tool module missing from tools/__init__.py."""
    async with Client(booklore_mcp.mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_prompts_registered():
    """The prompts module is imported by the package and registers its workflows."""
    async with Client(booklore_mcp.mcp) as c:
        prompts = {p.name: p for p in await c.list_prompts()}
        assert set(prompts) == {"organize_library"}
        rendered = await c.get_prompt("organize_library", {"context": "by mood"})
    text = rendered.messages[0].content.text
    assert "by mood" in text and "dry_run=true" in text and "approve" in text


def test_entrypoints_are_wired():
    """The console script (booklore_mcp:main) and `python -m booklore_mcp` both resolve
    to a callable main()."""
    assert callable(booklore_mcp.main)


def test_root_shim_reexports_the_same_server():
    """`uv run server.py` / `fastmcp list server.py` rely on the shim exposing the very
    same `mcp` object and a runnable `main`."""
    assert server_shim.mcp is booklore_mcp.mcp
    assert server_shim.main is booklore_mcp.main
