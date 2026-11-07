"""Aggregate/reporting tools: reading stats and library export."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from typing import Any, Literal

from .. import runtime
from ..app import mcp
from ..client import BookLoreError
from ..helpers import _all_books, _author_names, _book_kind, _meta, _shelf_names

# Flat field extractors for export_library's `fields` projection. Fields beyond the plain
# list view (asin, goodreadsId, googleId, hardcoverId, subtitle, description) are stripped
# by BookLore's list endpoint, so selecting them forces a withDescription fetch.
_EXPORT_FIELDS: dict[str, Any] = {
    "id": lambda b: b.get("id"),
    "title": lambda b: b.get("title") or _meta(b).get("title"),
    "subtitle": lambda b: _meta(b).get("subtitle"),
    "authors": lambda b: _author_names(_meta(b)),
    "series": lambda b: _meta(b).get("seriesName"),
    "seriesNumber": lambda b: _meta(b).get("seriesNumber"),
    "publisher": lambda b: _meta(b).get("publisher"),
    "publishedDate": lambda b: _meta(b).get("publishedDate"),
    "language": lambda b: _meta(b).get("language"),
    "pageCount": lambda b: _meta(b).get("pageCount"),
    "isbn10": lambda b: _meta(b).get("isbn10"),
    "isbn13": lambda b: _meta(b).get("isbn13"),
    "asin": lambda b: _meta(b).get("asin"),
    "goodreadsId": lambda b: _meta(b).get("goodreadsId"),
    "googleId": lambda b: _meta(b).get("googleId"),
    "hardcoverId": lambda b: _meta(b).get("hardcoverId"),
    "amazonRating": lambda b: _meta(b).get("amazonRating"),
    "goodreadsRating": lambda b: _meta(b).get("goodreadsRating"),
    "personalRating": lambda b: b.get("personalRating"),
    "readStatus": lambda b: b.get("readStatus"),
    "tags": lambda b: list(_meta(b).get("tags") or []),
    "categories": lambda b: list(_meta(b).get("categories") or []),
    "description": lambda b: _meta(b).get("description"),
    "shelves": _shelf_names,
    "kind": _book_kind,
}

# Export/missing fields BookLore omits from the plain list response (need withDescription).
_FULL_ONLY_FIELDS = {"asin", "goodreadsId", "googleId", "hardcoverId", "subtitle", "description"}


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Reading stats"})
async def get_reading_stats() -> dict:
    """Library-wide reading stats: counts by read status and average personal rating.

    Note: BookLore's list endpoint exposes readStatus / personalRating sparsely, so
    most books report as UNSET / unrated here."""
    books = await _all_books()
    by_status = Counter((b.get("readStatus") or "UNSET") for b in books)
    ratings = [b["personalRating"] for b in books if b.get("personalRating") is not None]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "total": len(books),
        "by_status": dict(by_status),
        "rated_count": len(ratings),
        "avg_rating": avg,
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Export library"})
async def export_library(
    format: Literal["json", "csv"] = "json",
    shelf_id: int | None = None,
    fields: list[str] | None = None,
    full: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """Export the library (or one shelf) as JSON or CSV.

    By default returns the trimmed summary (id/title/authors/series/readStatus/
    personalRating/shelves). To audit metadata completeness, pass `fields` to
    project a specific set, or `full=True` for every field. Selectable fields:
    id, title, subtitle, authors, series, seriesNumber, publisher, publishedDate,
    language, pageCount, isbn10, isbn13, asin, goodreadsId, googleId, hardcoverId,
    amazonRating, goodreadsRating, personalRating, readStatus, tags, categories,
    description, shelves, kind (book/comic/audiobook). Identifier fields
    (asin/goodreadsId/…) and description are fetched on demand.

    A full-library export easily exceeds the response token limit, so: prefer `csv`
    (far more compact than JSON), keep `fields` narrow, and page with `offset`/`limit`
    (the response reports `total` so you know how many pages remain). JSON output is
    compact (no indentation). Returns {format, total, offset, limit, returned, fields,
    content}.
    """
    if fields:
        bad = sorted(set(fields) - set(_EXPORT_FIELDS))
        if bad:
            raise BookLoreError(
                f"Unknown export field(s): {', '.join(bad)}. "
                f"Available: {', '.join(_EXPORT_FIELDS)}."
            )
        cols = list(fields)
    elif full:
        cols = list(_EXPORT_FIELDS)
    else:
        cols = ["id", "title", "authors", "series", "readStatus", "personalRating", "shelves"]

    need_desc = any(c in _FULL_ONLY_FIELDS for c in cols)
    if shelf_id is not None:
        books = await runtime.client.get(f"/api/v1/shelves/{shelf_id}/books") or []
        if need_desc:  # shelf endpoint returns list-trimmed records; hydrate by id
            full_by_id = {b.get("id"): b for b in await _all_books(with_description=True)}
            books = [full_by_id.get(b.get("id"), b) for b in books]
    else:
        books = await _all_books(with_description=need_desc)

    total = len(books)
    offset = max(0, offset)
    page = books[offset : offset + limit] if limit is not None else books[offset:]
    rows = [{c: _EXPORT_FIELDS[c](b) for c in cols} for b in page]

    if format == "json":
        content = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for row in rows:
            writer.writerow(
                ["; ".join(str(x) for x in v) if isinstance(v, list) else v for v in row.values()]
            )
        content = buf.getvalue()
    return {
        "format": format,
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "fields": cols,
        "content": content,
    }
