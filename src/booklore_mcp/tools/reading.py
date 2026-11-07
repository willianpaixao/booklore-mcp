"""Reading-state tools: read status, personal rating, and reading progress."""

from __future__ import annotations

from typing import Any, Literal, get_args

from fastmcp import Context

from .. import runtime
from ..app import mcp
from ..config import ReadStatus


@mcp.tool(annotations={"idempotentHint": True, "title": "Set read status"})
async def set_read_status(book_ids: list[int], status: ReadStatus) -> list[dict]:
    """Set the reading status for one or more books (e.g. READING, READ, UNREAD).
    Returns per-book update results."""
    return await runtime.client.post(
        "/api/v1/books/status", json={"bookIds": book_ids, "status": status}
    )


@mcp.tool(annotations={"idempotentHint": True, "title": "Set personal rating"})
async def set_personal_rating(book_ids: list[int], rating: int) -> list[dict]:
    """Set your personal rating for one or more books. Rating uses BookLore's
    configured scale (commonly 0-10 in half-star steps shown as 0-5 stars in the
    UI). Returns per-book update results."""
    return await runtime.client.put(
        "/api/v1/books/personal-rating", json={"ids": book_ids, "rating": rating}
    )


@mcp.tool(annotations={"idempotentHint": True, "title": "Set reading progress"})
async def set_reading_progress(
    book_id: int,
    book_file_id: int,
    progress_percent: float,
    position_href: str | None = None,
    position_data: str | None = None,
    date_finished: str | None = None,
) -> str:
    """Record reading progress for a book file.

    `book_file_id` is the file you're tracking — get it from get_book's
    `primaryFile.id` (or `alternativeFormats`). `progress_percent` is 0-100.
    `position_href` / `position_data` are optional format-specific resume
    pointers (e.g. an EPUB CFI). Pass `date_finished` as an ISO-8601 timestamp
    (e.g. "2026-06-09T12:00:00Z") to also mark the book finished.
    """
    body: dict[str, Any] = {
        "bookId": book_id,
        "fileProgress": {
            "bookFileId": book_file_id,
            "progressPercent": progress_percent,
            "positionHref": position_href,
            "positionData": position_data,
        },
    }
    if date_finished:
        body["dateFinished"] = date_finished
    await runtime.client.post("/api/v1/books/progress", json=body)
    return f"Set reading progress for book {book_id} to {progress_percent}%."


@mcp.tool(
    annotations={"destructiveHint": True, "idempotentHint": True, "title": "Reset reading progress"}
)
async def reset_progress(
    book_ids: list[int],
    source: Literal["BOOKLORE", "KOREADER", "KOBO"] = "BOOKLORE",
) -> list[dict]:
    """Clear reading progress for one or more books (max 500). `source` selects
    which progress to wipe: BOOKLORE (the built-in reader), KOREADER, or KOBO.
    This discards saved position — it cannot be undone."""
    return await runtime.client.post(
        "/api/v1/books/reset-progress", params={"type": source}, json=book_ids
    )


@mcp.tool(annotations={"idempotentHint": True, "title": "Bulk set read status"})
async def bulk_set_read_status(items: list[dict], ctx: Context | None = None) -> dict:
    """Set per-book reading status in bulk — e.g. importing a Goodreads CSV where each
    book has its own status. `items` is [{"book_id": int, "status": ReadStatus}], with
    status one of UNREAD/READING/RE_READING/READ/PARTIALLY_READ/PAUSED/WONT_READ/
    ABANDONED/UNSET. Books are grouped by status and sent in ONE call per distinct
    status (not one per book). Resolve title/author rows to book ids with match_books
    first. Returns {updated, by_status:{status:count}, failed}.
    """
    valid = set(get_args(ReadStatus))
    groups: dict[str, list[int]] = {}
    failed: list[dict] = []
    for entry in items:
        bid = entry.get("book_id")
        status = entry.get("status")
        if not isinstance(bid, int) or not isinstance(status, str) or status not in valid:
            failed.append({"book_id": bid, "error": f"invalid book_id/status: {entry}"})
            continue
        groups.setdefault(status, []).append(bid)

    updated: list[int] = []
    by_status: dict[str, int] = {}
    # One backend call per status group; report books processed as each lands.
    total = sum(len(ids) for ids in groups.values())
    for status, ids in groups.items():
        await runtime.client.post("/api/v1/books/status", json={"bookIds": ids, "status": status})
        updated.extend(ids)
        by_status[status] = len(ids)
        if ctx is not None:
            await ctx.report_progress(progress=len(updated), total=total)
    return {"updated": updated, "by_status": by_status, "failed": failed}
