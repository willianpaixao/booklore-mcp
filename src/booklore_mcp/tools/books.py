"""Read tools for individual books and recommendations."""

from __future__ import annotations

from .. import runtime
from ..app import mcp
from ..helpers import _all_books, _matches_query, _summarize_book
from ..models import BookSummary


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List / search books"}
)
async def list_books(query: str = "", limit: int = 25) -> list[BookSummary]:
    """Quick lookup: list books, optionally filtered by a keyword — the fastest way to
    find a book id.

    `query` is a case-insensitive substring over title/series/isbn/author names (the
    same matching as search_books' `query`); leave empty to list everything. Returns
    trimmed summaries (id, title, authors, series, readStatus, personalRating, shelves,
    kind) capped at `limit`, in library order. For filters, sorting or paging use
    search_books; for misspelled titles or subtitles use search_titles; call get_book
    for one book's full metadata.
    """
    books = await _all_books()
    if query:
        q = query.lower()
        books = [b for b in books if _matches_query(b, q)]
    return [_summarize_book(b) for b in books[: max(1, limit)]]


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get book details"})
async def get_book(book_id: int, with_description: bool = True) -> dict:
    """Fetch a single book's full record by ID, including its metadata.

    Use list_books first if you don't know the ID. Set with_description=False to
    omit the (potentially long) description field.
    """
    return await runtime.client.get(
        f"/api/v1/books/{book_id}",
        params={"withDescription": str(with_description).lower()},
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get recommendations"}
)
async def get_recommendations(book_id: int, limit: int = 10) -> list[dict]:
    """Get books recommended as similar to the given book (max 25)."""
    return (
        await runtime.client.get(
            f"/api/v1/books/{book_id}/recommendations",
            params={"limit": max(1, min(limit, 25))},
        )
        or []
    )
